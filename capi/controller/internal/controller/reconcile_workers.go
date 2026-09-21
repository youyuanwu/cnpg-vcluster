package controller

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"path"
	"regexp"
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

var importedImageDigest = regexp.MustCompile(`(?m)^[└├]──[^\n]*@(sha256:[0-9a-f]{64})`)

var offlineEgressEvidence = regexp.MustCompile(`(?m)^([0-9]+) ([0-9]+)$`)

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
		evidenceSet := normalizeWorkerEvidence(tenant.Status.WorkerContainers, containers, foundation.Cache.Generation)
		for index, container := range containers {
			if evidenceSet[index].Prepared {
				continue
			}
			evidence := evidenceSet[index]
			evidence, err = reconciler.prepareWorkerStep(ctx, container, foundation, evidence)
			if err != nil {
				return ctrl.Result{}, err
			}
			evidenceSet[index] = evidence
			return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
				status.WorkerContainers = evidenceSet
				replaceMachineIdentities(status, machines)
				return nil
			})
		}
		return ctrl.Result{Requeue: true}, reconciler.patchStatus(ctx, tenant.Name, func(status *tenancyv1alpha1.TenantStatus) error {
			status.WorkerContainers = evidenceSet
			replaceMachineIdentities(status, machines)
			status.Stage = tenancyv1alpha1.StageWorkersApplied
			setCondition(status, tenant, "WorkersReady", metav1.ConditionFalse, "PreCNIWorkersApplied", "Pre-CNI worker containers are prepared; Node readiness is not evaluated yet")
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

func (reconciler *TenantReconciler) prepareWorkerStep(ctx context.Context, container DockerContainer, foundation Foundation, evidence tenancyv1alpha1.WorkerContainerEvidence) (tenancyv1alpha1.WorkerContainerEvidence, error) {
	archives := make([]FoundationArchive, 0)
	for _, key := range requiredWorkerImageKeys {
		archive, found := archiveByKey(foundation.Cache.ImageArchives, key)
		if !found {
			return evidence, fmt.Errorf("worker image %s is missing from the foundation", key)
		}
		archives = append(archives, archive)
	}
	sort.Slice(archives, func(left, right int) bool { return archives[left].Key < archives[right].Key })
	for _, archive := range archives {
		if containsString(evidence.ImportedImages, archive.Key) {
			continue
		}
		if err := reconciler.prepareWorkerImage(ctx, container.ID, foundation, archive); err != nil {
			return evidence, err
		}
		evidence.ImportedImages = append(evidence.ImportedImages, archive.Key)
		sort.Strings(evidence.ImportedImages)
		return evidence, nil
	}
	if foundation.OfflineEnforced && !evidence.MirrorsConfigured {
		if err := reconciler.configureWorkerMirrors(ctx, container.ID, foundation); err != nil {
			return evidence, err
		}
		evidence.MirrorsConfigured = true
		return evidence, nil
	}
	if foundation.OfflineEnforced && !evidence.EgressVerified {
		if err := reconciler.installWorkerEgressRules(ctx, container.ID, foundation); err != nil {
			return evidence, err
		}
		evidence.EgressVerified = true
		return evidence, nil
	}
	if foundation.OfflineEnforced && !evidence.MirrorPullVerified {
		archive, found := foundationArchive(foundation, "CNPG_CONTROLLER_IMAGE")
		if !found {
			return evidence, fmt.Errorf("CNPG controller image is missing from the foundation")
		}
		if err := reconciler.execRequired(ctx, container.ID, []string{"crictl", "pull", archive.Reference}); err != nil {
			return evidence, fmt.Errorf("verify worker mirror pull: %w", err)
		}
		evidence.MirrorPullVerified = true
		return evidence, nil
	}
	evidence.Prepared = true
	return evidence, nil
}

func (reconciler *TenantReconciler) prepareWorkerImage(ctx context.Context, container string, foundation Foundation, archive FoundationArchive) error {
	archivePath := path.Join(foundation.Inputs.CacheContainerPath, "generations", foundation.Cache.Generation, archive.Path)
	result, err := reconciler.exec(ctx, container, []string{"sha256sum", archivePath})
	if err != nil {
		return fmt.Errorf("verify worker cache archive %s: %w", archive.Key, err)
	}
	fields := strings.Fields(result.Output)
	if result.ExitCode != 0 || len(fields) == 0 || fields[0] != archive.SHA256 {
		return fmt.Errorf("worker cache archive %s checksum mismatch", archive.Key)
	}
	for _, command := range [][]string{
		{"ctr", "--namespace", "k8s.io", "images", "import", "--digests", archivePath},
		{"ctr", "--namespace", "k8s.io", "images", "tag", "--force", archive.Tagged, archive.Reference},
		{"ctr", "--namespace", "k8s.io", "images", "tag", "--force", archive.Tagged, canonicalExactReference(archive)},
		{"ctr", "--namespace", "k8s.io", "images", "tag", "--force", archive.Tagged, runtimeDigestReference(archive)},
	} {
		if err := reconciler.execRequired(ctx, container, command); err != nil {
			return fmt.Errorf("prepare worker image %s: %w", archive.Key, err)
		}
	}
	inspect, err := reconciler.exec(ctx, container, []string{"ctr", "--namespace", "k8s.io", "images", "inspect", runtimeDigestReference(archive)})
	if err != nil || inspect.ExitCode != 0 {
		return fmt.Errorf("inspect imported worker image %s", archive.Key)
	}

	expectedDigest := archive.Reference[strings.LastIndex(archive.Reference, "@")+1:]
	match := importedImageDigest.FindStringSubmatch(inspect.Output)
	if len(match) != 2 || match[1] != expectedDigest {
		return fmt.Errorf("imported worker image %s digest mismatch", archive.Key)
	}
	return nil
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
	scripts := make([]string, 0, len(names))
	for _, registry := range names {
		server := "https://" + registry
		if registry == "docker.io" {
			server = "https://registry-1.docker.io"
		}
		content := fmt.Sprintf("server = %q\n\n[host.%q]\n  capabilities = [\"pull\", \"resolve\"]\n",
			server, fmt.Sprintf("http://%s:%d", foundation.Registry.Address, foundation.Registry.Port))
		directory := "/etc/containerd/certs.d/" + registry
		scripts = append(scripts, "umask 077; mkdir -p "+shellQuote(directory)+"; printf %s "+
			shellQuote(content)+" > "+shellQuote(directory+"/hosts.toml"))
	}
	return reconciler.execRequired(ctx, container, []string{"sh", "-ec", strings.Join(scripts, "; ")})
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
		captureShellExit("timeout 3 bash -c '</dev/tcp/1.1.1.1/443'") + "; " +
		"hits=$(iptables -L CAPI_OFFLINE -n -v -x | " +
		"awk '$1 ~ /^[0-9]+$/ && $9 == \"1.1.1.1\" {sum += $1} END {print sum + 0}'); " +
		"printf '%s %s\\n' \"$rc\" \"$hits\""
	result, err := reconciler.exec(ctx, container, []string{"sh", "-ec", probe})
	if err != nil {
		return err
	}
	var returnCode, hits int
	match := offlineEgressEvidence.FindStringSubmatch(result.Output)
	evidenceValid := len(match) == 3
	if evidenceValid {
		_, evidenceErr := fmt.Sscanf(match[0], "%d %d", &returnCode, &hits)
		evidenceValid = evidenceErr == nil
	}
	if !evidenceValid ||
		result.ExitCode != 0 || returnCode == 0 || hits < 1 || (returnCode != 1 && returnCode != 124) {
		return fmt.Errorf(
			"offline worker egress rejection was not proven: exit=%d evidence=%q",
			result.ExitCode,
			sanitize.Text(strings.TrimSpace(result.Output)),
		)
	}
	return nil
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

func captureShellExit(command string) string {
	return "if " + command + "; then rc=0; else rc=$?; fi"
}

func foundationArchive(foundation Foundation, key string) (FoundationArchive, bool) {
	for _, archive := range foundation.Cache.ImageArchives {
		if archive.Key == key {
			return archive, true
		}
	}
	return FoundationArchive{}, false
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

func workerEvidence(values []tenancyv1alpha1.WorkerContainerEvidence, container DockerContainer, generation string) tenancyv1alpha1.WorkerContainerEvidence {
	result := tenancyv1alpha1.WorkerContainerEvidence{
		Name:            container.Name,
		ID:              container.ID,
		CacheGeneration: generation,
	}
	for _, value := range values {
		if value.Name != container.Name {
			continue
		}
		if value.ID == container.ID && value.CacheGeneration == generation {
			return value
		}
		result.PreviousIDs = append(result.PreviousIDs, value.PreviousIDs...)
		if value.ID != "" && value.ID != container.ID {
			result.PreviousIDs = append(result.PreviousIDs, value.ID)
		}
		sort.Strings(result.PreviousIDs)
		return result
	}
	return result
}

func normalizeWorkerEvidence(values []tenancyv1alpha1.WorkerContainerEvidence, containers []DockerContainer, generation string) []tenancyv1alpha1.WorkerContainerEvidence {
	currentNames := map[string]struct{}{}
	for _, container := range containers {
		currentNames[container.Name] = struct{}{}
	}
	removed := make([]tenancyv1alpha1.WorkerContainerEvidence, 0)
	previousNames := map[string]struct{}{}
	for _, value := range values {
		previousNames[value.Name] = struct{}{}
		if _, present := currentNames[value.Name]; !present {
			removed = append(removed, value)
		}
	}
	sort.Slice(removed, func(left, right int) bool { return removed[left].Name < removed[right].Name })
	result := make([]tenancyv1alpha1.WorkerContainerEvidence, 0, len(containers))
	newIndexes := make([]int, 0)
	for _, container := range containers {
		evidence := workerEvidence(values, container, generation)
		if _, existed := previousNames[container.Name]; !existed {
			newIndexes = append(newIndexes, len(result))
		}
		result = append(result, evidence)
	}
	for index, resultIndex := range newIndexes {
		if index >= len(removed) {
			break
		}
		result[resultIndex].PreviousIDs = append(result[resultIndex].PreviousIDs, removed[index].PreviousIDs...)
		if removed[index].ID != "" && removed[index].ID != result[resultIndex].ID {
			result[resultIndex].PreviousIDs = append(result[resultIndex].PreviousIDs, removed[index].ID)
		}
		sort.Strings(result[resultIndex].PreviousIDs)
	}
	sort.Slice(result, func(left, right int) bool {
		return result[left].Name < result[right].Name
	})
	return result
}

func allWorkerEvidencePrepared(values []tenancyv1alpha1.WorkerContainerEvidence) bool {
	for _, value := range values {
		if !value.Prepared {
			return false
		}
	}
	return len(values) != 0
}

func replaceMachineIdentities(status *tenancyv1alpha1.TenantStatus, machines []*unstructured.Unstructured) {
	previous := make([]tenancyv1alpha1.ObservedResourceIdentity, 0)
	retained := make([]tenancyv1alpha1.ObservedResourceIdentity, 0, len(status.ObservedResources))
	for _, identity := range status.ObservedResources {
		if identity.Kind == machineGVK.Kind && identity.APIVersion == machineGVK.GroupVersion().String() {
			previous = append(previous, identity)
			continue
		}
		retained = append(retained, identity)
	}
	sort.Slice(previous, func(left, right int) bool { return previous[left].Name < previous[right].Name })
	previousByName := map[string]tenancyv1alpha1.ObservedResourceIdentity{}
	currentNames := map[string]struct{}{}
	for _, identity := range previous {
		previousByName[identity.Name] = identity
	}
	for _, machine := range machines {
		currentNames[machine.GetName()] = struct{}{}
	}
	removed := make([]tenancyv1alpha1.ObservedResourceIdentity, 0)
	for _, identity := range previous {
		if _, present := currentNames[identity.Name]; !present {
			removed = append(removed, identity)
		}
	}
	current := make([]tenancyv1alpha1.ObservedResourceIdentity, 0, len(machines))
	newIndexes := make([]int, 0)
	for _, machine := range machines {
		identity := identityFor(machine)
		if old, present := previousByName[identity.Name]; present {
			identity.PreviousUIDs = append(identity.PreviousUIDs, old.PreviousUIDs...)
			if old.UID != identity.UID {
				identity.PreviousUIDs = append(identity.PreviousUIDs, old.UID)
			}
			sort.Strings(identity.PreviousUIDs)
		} else {
			newIndexes = append(newIndexes, len(current))
		}
		current = append(current, identity)
	}
	sort.Slice(removed, func(left, right int) bool { return removed[left].Name < removed[right].Name })
	for index, currentIndex := range newIndexes {
		if index >= len(removed) {
			break
		}
		current[currentIndex].PreviousUIDs = append(current[currentIndex].PreviousUIDs, removed[index].PreviousUIDs...)
		if removed[index].UID != current[currentIndex].UID {
			current[currentIndex].PreviousUIDs = append(current[currentIndex].PreviousUIDs, removed[index].UID)
		}
		sort.Strings(current[currentIndex].PreviousUIDs)
	}
	status.ObservedResources = append(retained, current...)
	sort.Slice(status.ObservedResources, func(left, right int) bool {
		a := status.ObservedResources[left]
		b := status.ObservedResources[right]
		return a.APIVersion+"/"+a.Kind+"/"+a.Namespace+"/"+a.Name <
			b.APIVersion+"/"+b.Kind+"/"+b.Namespace+"/"+b.Name
	})
}

func machineInventoryMatches(status tenancyv1alpha1.TenantStatus, machines []*unstructured.Unstructured) bool {
	expected := map[string]string{}
	for _, identity := range status.ObservedResources {
		if identity.Kind == machineGVK.Kind && identity.APIVersion == machineGVK.GroupVersion().String() {
			expected[identity.Name] = identity.UID
		}
	}
	if len(expected) != len(machines) {
		return false
	}
	for _, machine := range machines {
		if expected[machine.GetName()] != string(machine.GetUID()) {
			return false
		}
	}
	return true
}
