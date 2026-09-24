package controller

import (
	"context"
	"errors"
	"fmt"
	"path"
	"sort"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/sanitize"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

var machineGVK = schema.GroupVersionKind{Group: "cluster.x-k8s.io", Version: "v1beta2", Kind: "Machine"}

var errWorkerRuntimePending = errors.New("worker runtime is pending")

func (reconciler *TenantReconciler) reconcileWorkers(ctx context.Context, tenant *tenancyv1alpha1.Tenant, canonical validation.CanonicalSpec, specHash string, foundation Foundation) (ctrl.Result, error) {
	resourceContext := resources.Context{
		Tenant:         tenant,
		Spec:           canonical,
		SpecHash:       specHash,
		FoundationHash: foundation.Hash,
		Endpoint:       tenant.Status.Endpoint,
		Inputs:         foundation.ResourceInputs(),
	}
	switch tenant.Status.Stage {
	case tenancyv1alpha1.StageBootstrapRBACApplied:
		volume, err := reconciler.ensureVolume(ctx, tenant, specHash, foundation)
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.DockerVolume = &tenancyv1alpha1.DockerVolumeIdentity{
				Name:       volume.Name,
				CreatedAt:  volume.CreatedAt,
				Mountpoint: volume.Mountpoint,
				Labels:     volume.Labels,
			}
			status.Stage = tenancyv1alpha1.StageVolumeCreated
			return nil
		})
	case tenancyv1alpha1.StageVolumeCreated:
		resourceContext.VolumePath = tenant.Status.DockerVolume.Mountpoint
		commands, err := workerBootstrapCommands(foundation)
		if err != nil {
			return ctrl.Result{}, err
		}
		resourceContext.WorkerBootstrapCommands = commands
		identity, err := reconciler.ensureUnstructured(ctx, resources.KubeadmConfigTemplate(resourceContext), tenant, specHash, foundation, "kubeadm-config-template")
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageKubeadmTemplateCreated, identity)
	case tenancyv1alpha1.StageKubeadmTemplateCreated:
		resourceContext.VolumePath = tenant.Status.DockerVolume.Mountpoint
		identity, err := reconciler.ensureUnstructured(ctx, resources.DevMachineTemplate(resourceContext), tenant, specHash, foundation, "dev-machine-template")
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageMachineTemplateCreated, identity)
	case tenancyv1alpha1.StageMachineTemplateCreated:
		resourceContext.VolumePath = tenant.Status.DockerVolume.Mountpoint
		identity, err := reconciler.ensureUnstructured(ctx, resources.MachineDeployment(resourceContext), tenant, specHash, foundation, "machine-deployment")
		if err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, reconciler.advanceWithIdentity(ctx, tenant, tenancyv1alpha1.StageMachineDeploymentCreated, identity)
	case tenancyv1alpha1.StageMachineDeploymentCreated:
		machines, containers, err := reconciler.observePreCNIWorkers(ctx, tenant, specHash, foundation)
		if err != nil {
			if errors.Is(err, errWorkerRuntimePending) {
				return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
			}
			return ctrl.Result{}, err
		}
		if len(machines) != int(canonical.Workers) || len(containers) != int(canonical.Workers) {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
		references := workerContainerReferences(containers)
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.WorkerContainers = references
			replaceMachineIdentities(status, machines)
			status.Stage = tenancyv1alpha1.StageWorkersApplied
			setCondition(status, tenant, "WorkersReady", metav1.ConditionFalse, "PreCNIWorkersApplied", "Pre-CNI worker containers are running; Node readiness is not evaluated yet")
			setCondition(status, tenant, "Ready", metav1.ConditionFalse, "Phase3Pending", "Tenant networking, storage, and database reconciliation are pending")
			return nil
		})
	case tenancyv1alpha1.StageWorkersApplied:
		return reconciler.reconcileNetwork(ctx, tenant, canonical, specHash, foundation)
	default:
		return reconciler.reconcileNetwork(ctx, tenant, canonical, specHash, foundation)
	}
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

func (reconciler *TenantReconciler) validateRecordedVolume(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) error {
	if tenant.Status.DockerVolume == nil {
		return nil
	}
	volume, err := reconciler.docker().InspectVolume(ctx, tenant.Status.DockerVolume.Name)
	if err != nil {
		return err
	}
	if volume == nil {
		return fmt.Errorf("recorded Docker volume is missing")
	}
	expected := map[string]string{
		foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
		"cnpg-vcluster.capi/role":                  "tenant-storage",
		"cnpg-vcluster.capi/tenant":                tenant.Name,
		"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
		"tenancy.cnpg-vcluster.io/spec-hash":       specHash,
		"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
	}
	if volume.Name != tenant.Status.DockerVolume.Name ||
		volume.CreatedAt != tenant.Status.DockerVolume.CreatedAt ||
		volume.Mountpoint != tenant.Status.DockerVolume.Mountpoint ||
		!stringMapEqual(volume.Labels, expected) {
		return fmt.Errorf("recorded Docker volume identity changed")
	}
	return nil
}

func (reconciler *TenantReconciler) observePreCNIWorkers(ctx context.Context, tenant *tenancyv1alpha1.Tenant, specHash string, foundation Foundation) ([]*unstructured.Unstructured, []DockerContainer, error) {
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(schema.GroupVersionKind{Group: machineGVK.Group, Version: machineGVK.Version, Kind: "MachineList"})
	if err := reconciler.reader().List(ctx, list, client.InNamespace(tenant.Name), client.MatchingLabels{"cluster.x-k8s.io/cluster-name": tenant.Name}); err != nil {
		return nil, nil, fmt.Errorf("list Tenant Machines: %w", err)
	}
	machineDeployment := findIdentity(tenant.Status, machineDeploymentGVK, tenant.Name, tenant.Name+"-worker")
	if machineDeployment == nil {
		return nil, nil, fmt.Errorf("MachineDeployment identity is not recorded")
	}
	machines := make([]*unstructured.Unstructured, 0, len(list.Items))
	machineNames := map[string]struct{}{}
	for index := range list.Items {
		machine := list.Items[index].DeepCopy()
		if err := validateRootOwnership(machine, tenant, specHash, foundation.Hash, "machine", foundation.Inputs.OwnershipLabel, foundation.Inputs.LabPrefix); err != nil {
			return nil, nil, err
		}
		if err := validateOwnerChain(ctx, reconciler.reader(), machine, *machineDeployment); err != nil {
			return nil, nil, err
		}
		machines = append(machines, machine)
		machineNames[machine.GetName()] = struct{}{}
	}
	containers, err := reconciler.docker().ListWorkerContainers(ctx, tenant.Name)
	if err != nil {
		return nil, nil, fmt.Errorf("list Tenant worker containers: %w", err)
	}
	for _, container := range containers {
		if container.State != "running" {
			return nil, nil, errWorkerRuntimePending
		}
		if _, expected := machineNames[container.Name]; !expected {
			return nil, nil, fmt.Errorf("worker container %s has no exact Machine", container.Name)
		}
		attached := false
		for _, networkID := range container.Networks {
			attached = attached || networkID == foundation.NetworkID
		}
		if !attached {
			return nil, nil, fmt.Errorf("worker container %s is not on the foundation network", container.Name)
		}
		result, err := reconciler.docker().Exec(ctx, container.ID, []string{"test", "-S", "/run/containerd/containerd.sock"})
		if err != nil {
			return nil, nil, err
		}
		if result.ExitCode != 0 {
			return nil, nil, errWorkerRuntimePending
		}
	}
	sort.Slice(machines, func(left, right int) bool { return machines[left].GetName() < machines[right].GetName() })
	sort.Slice(containers, func(left, right int) bool { return containers[left].Name < containers[right].Name })
	return machines, containers, nil
}

func workerBootstrapCommands(foundation Foundation) ([]string, error) {
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

func (reconciler *TenantReconciler) execRequired(ctx context.Context, container string, command []string) error {
	result, err := reconciler.exec(ctx, container, command)
	if err != nil {
		return err
	}

	if result.ExitCode != 0 {
		return fmt.Errorf("container command failed with exit %d: %s", result.ExitCode, sanitize.Text(result.Output))
	}
	return nil
}

func (reconciler *TenantReconciler) exec(ctx context.Context, container string, command []string) (DockerExecResult, error) {
	bounded := append([]string{"timeout", "90"}, command...)
	return reconciler.docker().Exec(ctx, container, bounded)
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

func workerContainerReferences(containers []DockerContainer) []tenancyv1alpha1.WorkerContainerEvidence {
	result := make([]tenancyv1alpha1.WorkerContainerEvidence, 0, len(containers))
	for _, container := range containers {
		result = append(result, tenancyv1alpha1.WorkerContainerEvidence{Name: container.Name, ID: container.ID})
	}
	sort.Slice(result, func(left, right int) bool {
		return result[left].Name < result[right].Name
	})
	return result
}

func replaceMachineIdentities(status *tenancyv1alpha1.TenantStatus, machines []*unstructured.Unstructured) {
	status.ObservedResources = replaceResourceIdentities(status.ObservedResources, machineGVK, machines)
}

func replaceResourceIdentities(
	values []tenancyv1alpha1.ObservedResourceIdentity,
	gvk schema.GroupVersionKind,
	objects []*unstructured.Unstructured,
) []tenancyv1alpha1.ObservedResourceIdentity {
	retained := make([]tenancyv1alpha1.ObservedResourceIdentity, 0, len(values))
	for _, identity := range values {
		if identity.Kind == gvk.Kind && identity.APIVersion == gvk.GroupVersion().String() {
			continue
		}
		retained = append(retained, identity)
	}
	current := make([]tenancyv1alpha1.ObservedResourceIdentity, 0, len(objects))
	for _, object := range objects {
		current = append(current, identityFor(object))
	}
	result := append(retained, current...)
	sort.Slice(result, func(left, right int) bool {
		a := result[left]
		b := result[right]
		return a.APIVersion+"/"+a.Kind+"/"+a.Namespace+"/"+a.Name <
			b.APIVersion+"/"+b.Kind+"/"+b.Namespace+"/"+b.Name
	})
	return result
}
