package controller

import (
	"context"
	"errors"
	"fmt"
	"net/url"
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
		for _, container := range containers {
			if evidencePrepared(tenant.Status.WorkerContainers, container.ID, foundation.Cache.Generation) {
				continue
			}
			if err := reconciler.prepareWorker(ctx, container, foundation); err != nil {
				return ctrl.Result{}, err
			}
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				upsertWorkerEvidence(status, tenancyv1alpha1.WorkerContainerEvidence{
					Name:            container.Name,
					ID:              container.ID,
					CacheGeneration: foundation.Cache.Generation,
					Prepared:        true,
				})
				for _, machine := range machines {
					if err := upsertIdentity(status, identityFor(machine)); err != nil {
						return err
					}
				}
				return nil
			})
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.Stage = tenancyv1alpha1.StageWorkersApplied
			setCondition(status, tenant, "WorkersReady", metav1.ConditionFalse, "PreCNIWorkersApplied", "Pre-CNI worker containers are prepared; Node readiness is not evaluated yet")
			setCondition(status, tenant, "Ready", metav1.ConditionFalse, "Phase3Pending", "Tenant networking, storage, and database reconciliation are pending")
			return nil
		})
	case tenancyv1alpha1.StageWorkersApplied:
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	default:
		return ctrl.Result{}, fmt.Errorf("unsupported Tenant lifecycle stage %q", tenant.Status.Stage)
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

func (reconciler *TenantReconciler) prepareWorker(ctx context.Context, container DockerContainer, foundation Foundation) error {
	for _, archive := range foundation.Cache.ImageArchives {
		if !archive.Worker {
			continue
		}
		archivePath := path.Join(foundation.Inputs.CacheContainerPath, "generations", foundation.Cache.Generation, archive.Path)
		result, err := reconciler.docker().Exec(ctx, container.ID, []string{"sha256sum", archivePath})
		fields := strings.Fields(result.Output)
		if err != nil || result.ExitCode != 0 || len(fields) == 0 || fields[0] != archive.SHA256 {
			return fmt.Errorf("worker cache archive %s checksum mismatch", archive.Key)
		}
		for _, command := range [][]string{
			{"ctr", "--namespace", "k8s.io", "images", "import", "--digests", archivePath},
			{"ctr", "--namespace", "k8s.io", "images", "tag", "--force", archive.Tagged, archive.Reference},
			{"ctr", "--namespace", "k8s.io", "images", "inspect", archive.Reference},
		} {
			if err := reconciler.execRequired(ctx, container.ID, command); err != nil {
				return fmt.Errorf("prepare worker image %s: %w", archive.Key, err)
			}
		}
	}
	if foundation.OfflineEnforced {
		if err := reconciler.configureWorkerMirrors(ctx, container.ID, foundation); err != nil {
			return err
		}
		if err := reconciler.installWorkerEgressRules(ctx, container.ID, foundation); err != nil {
			return err
		}
		for _, archive := range foundation.Cache.ImageArchives {
			if archive.Key == "CNPG_CONTROLLER_IMAGE" {
				if err := reconciler.execRequired(ctx, container.ID, []string{"crictl", "pull", archive.Reference}); err != nil {
					return fmt.Errorf("verify worker mirror pull: %w", err)
				}
			}
		}
	}
	return nil
}

func (reconciler *TenantReconciler) configureWorkerMirrors(ctx context.Context, container string, foundation Foundation) error {
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
		script := "umask 077; mkdir -p " + shellQuote(directory) + "; printf %s " +
			shellQuote(content) + " > " + shellQuote(directory+"/hosts.toml")
		if err := reconciler.execRequired(ctx, container, []string{"sh", "-ec", script}); err != nil {
			return fmt.Errorf("configure worker registry mirror for %s: %w", registry, err)
		}
	}
	return nil
}

func (reconciler *TenantReconciler) installWorkerEgressRules(ctx context.Context, container string, foundation Foundation) error {
	commands := []string{
		"iptables -N CAPI_OFFLINE 2>/dev/null || true",
		"iptables -F CAPI_OFFLINE",
	}
	for _, subnet := range foundation.AllowedSubnets {
		commands = append(commands, "iptables -A CAPI_OFFLINE -d "+shellQuote(subnet)+" -j RETURN")
	}
	commands = append(commands,
		"iptables -A CAPI_OFFLINE -d 1.1.1.1/32 -p tcp --dport 443 -j REJECT",
		"iptables -A CAPI_OFFLINE -p tcp -m multiport --dports 80,443 -j REJECT",
		"iptables -A CAPI_OFFLINE -j RETURN",
		"iptables -C OUTPUT -j CAPI_OFFLINE 2>/dev/null || iptables -I OUTPUT 1 -j CAPI_OFFLINE",
		"iptables -C CAPI_OFFLINE -d 1.1.1.1/32 -p tcp --dport 443 -j REJECT",
	)
	if err := reconciler.execRequired(ctx, container, []string{"sh", "-ec", strings.Join(commands, "; ")}); err != nil {
		return err
	}
	probe := "iptables -Z CAPI_OFFLINE; " +
		"timeout 3 bash -c '</dev/tcp/1.1.1.1/443'; rc=$?; " +
		"hits=$(iptables -L CAPI_OFFLINE -n -v -x | " +
		"awk '$1 ~ /^[0-9]+$/ && $9 == \"1.1.1.1\" {sum += $1} END {print sum + 0}'); " +
		"printf '%s %s\\n' \"$rc\" \"$hits\""
	result, err := reconciler.docker().Exec(ctx, container, []string{"sh", "-ec", probe})
	if err != nil {
		return err
	}
	var returnCode, hits int
	if _, err := fmt.Sscanf(strings.TrimSpace(result.Output), "%d %d", &returnCode, &hits); err != nil ||
		result.ExitCode != 0 || returnCode == 0 || hits < 1 || (returnCode != 1 && returnCode != 124) {
		return fmt.Errorf("offline worker egress rejection was not proven")
	}
	return nil
}

func (reconciler *TenantReconciler) execRequired(ctx context.Context, container string, command []string) error {
	result, err := reconciler.docker().Exec(ctx, container, command)
	if err != nil {
		return err
	}
	if result.ExitCode != 0 {
		return fmt.Errorf("container command failed with exit %d: %s", result.ExitCode, sanitize.Text(result.Output))
	}
	return nil
}

func imageRegistry(reference string) string {
	value := strings.Split(reference, "@")[0]
	if !strings.Contains(value, "/") {
		return "docker.io"
	}
	parsed, err := url.Parse("//" + value)
	first := strings.Split(value, "/")[0]
	if err == nil && parsed.Host != "" {
		first = parsed.Host
	}
	if strings.ContainsAny(first, ".:") || first == "localhost" {
		return first
	}
	return "docker.io"
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

func evidencePrepared(values []tenancyv1alpha1.WorkerContainerEvidence, id, generation string) bool {
	for _, value := range values {
		if value.ID == id && value.CacheGeneration == generation && value.Prepared {
			return true
		}
	}
	return false
}

func upsertWorkerEvidence(status *tenancyv1alpha1.TenantStatus, evidence tenancyv1alpha1.WorkerContainerEvidence) {
	for index := range status.WorkerContainers {
		if status.WorkerContainers[index].Name == evidence.Name {
			status.WorkerContainers[index] = evidence
			return
		}
	}
	status.WorkerContainers = append(status.WorkerContainers, evidence)
	sort.Slice(status.WorkerContainers, func(left, right int) bool {
		return status.WorkerContainers[left].Name < status.WorkerContainers[right].Name
	})
}
