package controller

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/url"
	"sort"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

var errTenantAdministrativeAccessPending = errors.New("Tenant administrative access is pending")

type TenantClientFactory interface {
	ClientFor([]byte, string) (client.Client, error)
}

type tenantClientFactory struct{}

func (tenantClientFactory) ClientFor(kubeconfig []byte, endpoint string) (client.Client, error) {
	configuration, err := clientcmd.Load(kubeconfig)
	if err != nil {
		return nil, fmt.Errorf("decode Tenant kubeconfig: %w", err)
	}
	current := configuration.Contexts[configuration.CurrentContext]
	if current == nil || current.Cluster == "" || current.AuthInfo == "" {
		return nil, fmt.Errorf("Tenant kubeconfig current context is incomplete")
	}
	cluster := configuration.Clusters[current.Cluster]
	auth := configuration.AuthInfos[current.AuthInfo]
	if cluster == nil || auth == nil || len(cluster.CertificateAuthorityData) == 0 {
		return nil, fmt.Errorf("Tenant kubeconfig cluster or CA data is incomplete")
	}
	server, err := url.Parse(cluster.Server)
	if err != nil || server.Scheme != "https" || server.Host != endpoint || server.Path != "" {
		return nil, fmt.Errorf("Tenant kubeconfig endpoint does not match the allocated endpoint")
	}
	if len(auth.ClientCertificateData) == 0 || len(auth.ClientKeyData) == 0 {
		return nil, fmt.Errorf("Tenant kubeconfig client credentials are incomplete")
	}
	restConfig, err := clientcmd.RESTConfigFromKubeConfig(kubeconfig)
	if err != nil {
		return nil, fmt.Errorf("build Tenant REST configuration: %w", err)
	}
	restConfig.Timeout = 30 * time.Second
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		return nil, err
	}
	if err := rbacv1.AddToScheme(scheme); err != nil {
		return nil, err
	}
	return client.New(restConfig, client.Options{Scheme: scheme})
}

func tenantClientFromSecret(ctx context.Context, reader client.Reader, factory TenantClientFactory, namespace, tenantName, endpoint string) (client.Client, *corev1.Secret, error) {
	var secret corev1.Secret
	if err := reader.Get(ctx, types.NamespacedName{Namespace: namespace, Name: tenantName + "-kubeconfig"}, &secret); err != nil {
		return nil, nil, err
	}
	if secret.Type != corev1.SecretType("cluster.x-k8s.io/secret") {
		return nil, nil, fmt.Errorf("Tenant kubeconfig Secret type is unexpected")
	}
	value := secret.Data["value"]
	if len(value) == 0 {
		return nil, nil, fmt.Errorf("Tenant kubeconfig Secret has no value")
	}
	tenantClient, err := factory.ClientFor(bytes.Clone(value), endpoint)
	if err != nil {
		return nil, nil, err
	}
	return tenantClient, &secret, nil
}

func applyBootstrapRBAC(ctx context.Context, tenantClient client.Client) error {
	for _, raw := range resources.BootstrapRBAC() {
		object := raw.(client.Object)
		current, err := emptyBootstrapObject(object)
		if err != nil {
			return err
		}
		key := client.ObjectKeyFromObject(object)
		err = tenantClient.Get(ctx, key, current)
		if apierrors.IsForbidden(err) {
			return errTenantAdministrativeAccessPending
		}
		if apierrors.IsNotFound(err) {
			if err := tenantClient.Create(ctx, object); err != nil {
				if apierrors.IsForbidden(err) {
					return errTenantAdministrativeAccessPending
				}
				return fmt.Errorf("create Tenant bootstrap %T: %w", object, err)
			}
			continue
		}
		if err != nil {
			return fmt.Errorf("read Tenant bootstrap %T: %w", object, err)
		}
		switch desired := object.(type) {
		case *rbacv1.Role:
			existing := current.(*rbacv1.Role)
			if !equality.Semantic.DeepEqual(existing.Rules, desired.Rules) {
				desired.ResourceVersion = existing.ResourceVersion
				if err := tenantClient.Update(ctx, desired); err != nil {
					if apierrors.IsForbidden(err) {
						return errTenantAdministrativeAccessPending
					}
					return fmt.Errorf("update Tenant bootstrap Role %s: %w", desired.Name, err)
				}
			}
		case *rbacv1.RoleBinding:
			existing := current.(*rbacv1.RoleBinding)
			if !equality.Semantic.DeepEqual(existing.RoleRef, desired.RoleRef) ||
				!bootstrapSubjectsEqual(existing.Subjects, desired.Subjects) {
				desired.ResourceVersion = existing.ResourceVersion
				if err := tenantClient.Update(ctx, desired); err != nil {
					if apierrors.IsForbidden(err) {
						return errTenantAdministrativeAccessPending
					}
					return fmt.Errorf("update Tenant bootstrap RoleBinding %s: %w", desired.Name, err)
				}
			}
		}
	}
	return nil
}

func deleteBootstrapRBAC(ctx context.Context, tenantClient client.Client) (bool, error) {
	for _, raw := range resources.BootstrapRBAC() {
		desired := raw.(client.Object)
		key := client.ObjectKeyFromObject(desired)
		current, err := emptyBootstrapObject(desired)
		if err != nil {
			return false, err
		}
		err = tenantClient.Get(ctx, key, current)
		if apierrors.IsNotFound(err) {
			continue
		}
		if err != nil {
			return false, fmt.Errorf("inspect Tenant bootstrap RBAC: %w", err)
		}
		switch expected := desired.(type) {
		case *rbacv1.Role:
			actual := current.(*rbacv1.Role)
			if !equality.Semantic.DeepEqual(actual.Rules, expected.Rules) {
				return false, fmt.Errorf("Tenant bootstrap Role %s ownership cannot be proven", expected.Name)
			}
		case *rbacv1.RoleBinding:
			actual := current.(*rbacv1.RoleBinding)
			if !equality.Semantic.DeepEqual(actual.RoleRef, expected.RoleRef) ||
				!bootstrapSubjectsEqual(actual.Subjects, expected.Subjects) {
				return false, fmt.Errorf("Tenant bootstrap RoleBinding %s ownership cannot be proven", expected.Name)
			}
		default:
			return false, fmt.Errorf("unsupported Tenant bootstrap object %T", desired)
		}
		if current.GetDeletionTimestamp() != nil {
			return false, nil
		}
		uid := current.GetUID()
		resourceVersion := current.GetResourceVersion()
		if err := tenantClient.Delete(ctx, current, &client.DeleteOptions{
			Preconditions: &metav1.Preconditions{UID: &uid, ResourceVersion: &resourceVersion},
		}); err != nil && !apierrors.IsNotFound(err) && !apierrors.IsConflict(err) {
			return false, fmt.Errorf("delete Tenant bootstrap RBAC: %w", err)
		}
		return false, nil
	}
	return true, nil
}

func emptyBootstrapObject(object client.Object) (client.Object, error) {
	switch object.(type) {
	case *rbacv1.Role:
		return &rbacv1.Role{}, nil
	case *rbacv1.RoleBinding:
		return &rbacv1.RoleBinding{}, nil
	default:
		return nil, fmt.Errorf("unsupported Tenant bootstrap object %T", object)
	}
}

func bootstrapSubjectsEqual(left, right []rbacv1.Subject) bool {
	if len(left) != len(right) {
		return false
	}
	key := func(subject rbacv1.Subject) string {
		return subject.APIGroup + "/" + subject.Kind + "/" + subject.Namespace + "/" + subject.Name
	}
	leftKeys := make([]string, 0, len(left))
	rightKeys := make([]string, 0, len(right))
	for _, subject := range left {
		leftKeys = append(leftKeys, key(subject))
	}
	for _, subject := range right {
		rightKeys = append(rightKeys, key(subject))
	}
	sort.Strings(leftKeys)
	sort.Strings(rightKeys)
	return equality.Semantic.DeepEqual(leftKeys, rightKeys)
}
