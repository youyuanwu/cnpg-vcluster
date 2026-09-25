package controller

import (
	"context"
	"fmt"
	"path"
	"sort"
	"strings"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

func (reconciler *TenantReconciler) reconcileWorkers(
	ctx context.Context,
	tenantClient client.Client,
	tenant *tenancyv1alpha1.Tenant,
	canonical validation.CanonicalSpec,
	specHash string,
	foundation Foundation,
) (ctrl.Result, error) {
	volume, err := reconciler.ensureVolume(ctx, tenant, specHash, foundation)
	if err != nil {
		return ctrl.Result{}, err
	}
	resourceContext := serviceResourceContext(tenant, canonical, specHash, foundation)
	resourceContext.VolumePath = volume.Mountpoint
	commands, err := workerBootstrapCommands(foundation, canonical.DatabaseCount)
	if err != nil {
		return ctrl.Result{}, err
	}
	resourceContext.WorkerBootstrapCommands = commands
	for _, desired := range []struct {
		object   *unstructured.Unstructured
		resource string
	}{
		{resources.KubeadmConfigTemplate(resourceContext), "kubeadm-config-template"},
		{resources.DevMachineTemplate(resourceContext), "dev-machine-template"},
		{resources.MachineDeployment(resourceContext), "machine-deployment"},
	} {
		if _, changed, err := reconciler.ensureManagementObject(ctx, desired.object, tenant, specHash, foundation, desired.resource); err != nil {
			return ctrl.Result{}, err
		} else if changed {
			return progressRequeue(), nil
		}
	}
	return reconciler.reconcileNetwork(ctx, tenantClient, tenant, canonical, specHash, foundation)
}

func (reconciler *TenantReconciler) ensureVolume(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) (DockerVolume, error) {
	name := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
	expected := map[string]string{
		foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
		"cnpg-vcluster.capi/role":                  "tenant-storage",
		"cnpg-vcluster.capi/tenant":                tenant.Name,
		"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
		"tenancy.cnpg-vcluster.io/spec-hash":       specHash,
		"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
	}

	volume, err := reconciler.docker().InspectVolume(ctx, name)
	if err != nil {
		return DockerVolume{}, err
	}
	if volume == nil {
		created, err := reconciler.docker().CreateVolume(ctx, name, expected)
		if err != nil {
			return DockerVolume{}, err
		}
		volume = &created
	}
	if volume.Name != name || volume.CreatedAt == "" || volume.Mountpoint == "" || !stringMapEqual(volume.Labels, expected) {
		return DockerVolume{}, fmt.Errorf("Docker volume ownership identity mismatch")
	}
	return *volume, nil
}

func workerBootstrapCommands(foundation Foundation, databaseCount int32) ([]string, error) {
	archives := make([]FoundationArchive, 0)
	for _, key := range requiredWorkerImageKeys {
		archive, found := archiveByKey(foundation.Cache.ImageArchives, key)
		if !found {
			return nil, fmt.Errorf("worker image %s is missing from the foundation", key)
		}
		archives = append(archives, archive)
	}
	sort.Slice(archives, func(left, right int) bool { return archives[left].Key < archives[right].Key })
	commands := make([]string, 0, len(archives)*5)
	for _, archive := range archives {
		archivePath := path.Join(foundation.Inputs.CacheContainerPath, "generations", foundation.Cache.Generation, archive.Path)
		commands = append(commands,
			"printf '%s  %s\\n' "+shellQuote(archive.SHA256)+" "+shellQuote(archivePath)+" | sha256sum -c -",
			"ctr --namespace k8s.io images import --digests "+shellQuote(archivePath),
			"ctr --namespace k8s.io images tag --force "+shellQuote(archive.Tagged)+" "+shellQuote(archive.Reference),
			"ctr --namespace k8s.io images tag --force "+shellQuote(archive.Tagged)+" "+shellQuote(canonicalExactReference(archive)),
			"ctr --namespace k8s.io images tag --force "+shellQuote(archive.Tagged)+" "+shellQuote(runtimeDigestReference(archive)),
		)
	}
	for ordinal := int32(1); ordinal <= databaseCount; ordinal++ {
		directory := shellQuote(fmt.Sprintf("%s/volumes/cnpg/%d", foundation.Inputs.StorageContainerPath, ordinal))
		commands = append(commands, "mkdir -p "+directory+" && chown 26:26 "+directory+" && chmod 0700 "+directory)
	}
	if !foundation.OfflineEnforced {
		return commands, nil
	}
	registries := map[string]struct{}{}
	for _, archive := range foundation.Cache.ImageArchives {
		if archive.Worker {
			registries[imageRegistry(archive.Tagged)] = struct{}{}
		}
	}
	names := make([]string, 0, len(registries))
	for registry := range registries {
		names = append(names, registry)
	}
	sort.Strings(names)
	for _, registry := range names {
		server := "https://" + registry
		if registry == "docker.io" {
			server = "https://registry-1.docker.io"
		}
		content := fmt.Sprintf("server = %q\n\n[host.%q]\n  capabilities = [\"pull\", \"resolve\"]\n",
			server, fmt.Sprintf("http://%s:%d", foundation.Registry.Address, foundation.Registry.Port))
		directory := "/etc/containerd/certs.d/" + registry
		commands = append(commands, "umask 077; mkdir -p "+shellQuote(directory)+"; printf %s "+
			shellQuote(content)+" > "+shellQuote(directory+"/hosts.toml"))
	}
	commands = append(commands,
		"iptables -N CAPI_OFFLINE 2>/dev/null || true",
		"iptables -F CAPI_OFFLINE",
	)
	for _, subnet := range foundation.AllowedSubnets {
		commands = append(commands, "iptables -A CAPI_OFFLINE -d "+shellQuote(subnet)+" -j RETURN")
	}
	commands = append(commands,
		"iptables -A CAPI_OFFLINE -p tcp -m multiport --dports 80,443 -j REJECT",
		"iptables -A CAPI_OFFLINE -j RETURN",
		"iptables -C OUTPUT -j CAPI_OFFLINE 2>/dev/null || iptables -I OUTPUT 1 -j CAPI_OFFLINE",
	)
	return commands, nil
}

func canonicalExactReference(archive FoundationArchive) string {
	return archive.Tagged + "@" + archive.Reference[strings.LastIndex(archive.Reference, "@")+1:]
}

func runtimeDigestReference(archive FoundationArchive) string {
	tagged := archive.Tagged
	lastSlash := strings.LastIndex(tagged, "/")
	lastColon := strings.LastIndex(tagged, ":")
	if lastColon > lastSlash {
		tagged = tagged[:lastColon]
	}
	return tagged + "@" + archive.Reference[strings.LastIndex(archive.Reference, "@")+1:]
}

func imageRegistry(reference string) string {
	value := strings.Split(reference, "@")[0]
	first := strings.Split(value, "/")[0]
	if !strings.Contains(value, "/") || (!strings.ContainsAny(first, ".:") && first != "localhost") {
		return "docker.io"
	}
	return first
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func stringMapEqual(left, right map[string]string) bool {
	if len(left) != len(right) {
		return false
	}
	for key, value := range left {
		if right[key] != value {
			return false
		}
	}
	return true
}
