package controller

import (
	"context"
	"errors"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

type conflictClient struct {
	client.Client
	patches int
}

func (value *conflictClient) Status() client.StatusWriter {
	return &conflictStatusWriter{client: value, delegate: value.Client.Status()}
}

type conflictStatusWriter struct {
	client   *conflictClient
	delegate client.StatusWriter
}

func (writer *conflictStatusWriter) Create(ctx context.Context, object client.Object, subResource client.Object, options ...client.SubResourceCreateOption) error {
	return writer.delegate.Create(ctx, object, subResource, options...)
}

func (writer *conflictStatusWriter) Update(ctx context.Context, object client.Object, options ...client.SubResourceUpdateOption) error {
	return writer.delegate.Update(ctx, object, options...)
}

func (writer *conflictStatusWriter) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.SubResourcePatchOption) error {
	writer.client.patches++
	if writer.client.patches == 1 {
		var current tenancyv1alpha1.Tenant
		if err := writer.client.Client.Get(ctx, client.ObjectKey{Name: object.GetName()}, &current); err != nil {
			return err
		}
		current.Status.Endpoint = "concurrent:6443"
		if err := writer.client.Client.Status().Update(ctx, &current); err != nil {
			return err
		}
		return apierrors.NewConflict(
			schema.GroupResource{Group: tenancyv1alpha1.GroupVersion.Group, Resource: "tenants"},
			object.GetName(),
			errors.New("injected conflict"),
		)
	}
	return writer.delegate.Patch(ctx, object, patch, options...)
}

func (writer *conflictStatusWriter) Apply(ctx context.Context, object runtime.ApplyConfiguration, options ...client.SubResourceApplyOption) error {
	return writer.delegate.Apply(ctx, object, options...)
}

func TestStatusPatchRetriesConflictAndPreservesConcurrentFields(t *testing.T) {
	scheme := testScheme(t)
	tenant := validTenant("tenant-a")
	base := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(tenant).WithObjects(tenant).Build()
	kubernetes := &conflictClient{Client: base}
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes}
	if err := reconciler.patchStatus(context.Background(), tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
		status.Phase = tenancyv1alpha1.PhaseProgressing
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if kubernetes.patches != 2 {
		t.Fatalf("expected one conflict retry, got %d patches", kubernetes.patches)
	}
	var updated tenancyv1alpha1.Tenant
	if err := base.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &updated); err != nil {
		t.Fatal(err)
	}
	if updated.Status.Endpoint != "concurrent:6443" || updated.Status.Phase != tenancyv1alpha1.PhaseProgressing {
		t.Fatalf("concurrent status was overwritten: %#v", updated.Status)
	}
}

func validTenant(name string) *tenancyv1alpha1.Tenant {
	return &tenancyv1alpha1.Tenant{
		TypeMeta:   metav1.TypeMeta{APIVersion: tenancyv1alpha1.GroupVersion.String(), Kind: "Tenant"},
		ObjectMeta: metav1.ObjectMeta{Name: name, UID: "tenant-uid", Generation: 1},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
	}
}
