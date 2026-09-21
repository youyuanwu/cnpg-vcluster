package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/validation"
)

type fakeDockerClient struct {
	container  DockerContainer
	containers map[string]DockerContainer
	network    DockerNetwork
	volumes    map[string]DockerVolume
	workers    []DockerContainer
	execResult DockerExecResult
	execFunc   func([]string) (DockerExecResult, error)
	commands   [][]string
	removed    []string
	err        error
}

func (fake *fakeDockerClient) InspectContainer(_ context.Context, id string) (DockerContainer, error) {
	if container, present := fake.containers[id]; present {
		return container, fake.err
	}
	return fake.container, fake.err
}

func (fake *fakeDockerClient) InspectNetwork(context.Context, string) (DockerNetwork, error) {
	return fake.network, fake.err
}

func (fake *fakeDockerClient) InspectVolume(_ context.Context, name string) (*DockerVolume, error) {
	if fake.err != nil {
		return nil, fake.err
	}
	volume, exists := fake.volumes[name]
	if !exists {
		return nil, nil
	}
	return &volume, nil
}

func (fake *fakeDockerClient) CreateVolume(_ context.Context, name string, labels map[string]string) (DockerVolume, error) {
	if fake.err != nil {
		return DockerVolume{}, fake.err
	}
	volume := DockerVolume{Name: name, CreatedAt: "now", Mountpoint: "/var/lib/docker/" + name, Labels: labels}
	if fake.volumes == nil {
		fake.volumes = map[string]DockerVolume{}
	}
	fake.volumes[name] = volume
	return volume, nil
}

func (fake *fakeDockerClient) RemoveVolume(_ context.Context, name string) error {
	if fake.err != nil {
		return fake.err
	}
	delete(fake.volumes, name)
	fake.removed = append(fake.removed, name)
	return nil
}

func (fake *fakeDockerClient) ListWorkerContainers(context.Context, string) ([]DockerContainer, error) {
	return fake.workers, fake.err
}

func (fake *fakeDockerClient) Exec(_ context.Context, _ string, command []string) (DockerExecResult, error) {
	fake.commands = append(fake.commands, append([]string(nil), command...))
	if fake.execFunc != nil {
		return fake.execFunc(command)
	}
	return fake.execResult, fake.err
}

func testFoundation() Foundation {
	active := "{\"generation\":\"generation\",\"schema\":1}\n"
	activeDigest := sha256.Sum256([]byte(active))
	foundation := Foundation{
		Schema:                2,
		ManagementContainerID: "management-id",
		ManagementLabels: map[string]string{
			"io.x-k8s.kind.cluster": "management",
			"io.x-k8s.kind.role":    "control-plane",
		},
		NetworkID:         "network-id",
		Subnet:            "172.18.0.0/16",
		PoolStart:         "172.18.255.1",
		PoolEnd:           "172.18.255.3",
		ReservedCIDRs:     []string{"10.0.0.0/16"},
		AllowedSubnets:    []string{"10.0.0.0/16", "127.0.0.0/8", "172.18.0.0/16"},
		KubernetesVersion: "v1.36.4",
		ControllerImage:   "controller:test",
		MutationEnabled:   true,
		Versions: map[string]string{
			"GO_VERSION":                 "1.27.1",
			"KUBERNETES_VERSION":         "v1.36.4",
			"CAPI_VERSION":               "v1.14.1",
			"KAMAJI_CAPI_VERSION":        "v0.20.0",
			"CONTROLLER_RUNTIME_VERSION": "v0.24.1",
			"CONTROLLER_TOOLS_VERSION":   "v0.21.0",
		},
		Cache: FoundationCache{
			Generation:   "generation",
			StateSHA256:  strings.Repeat("a", 64),
			ActiveSHA256: hex.EncodeToString(activeDigest[:]),
		},
		Inputs: FoundationInputs{
			OwnershipLabel:          "example.io/owned",
			LabPrefix:               "example",
			APIPort:                 6443,
			ClusterDomain:           "example.local",
			NodeImage:               "kindest/node:v1",
			CacheHostPath:           "/cache",
			CacheContainerPath:      "/var/lib/capi-image-cache",
			StorageContainerPath:    "/var/lib/storage",
			KonnectivityServerImage: "example/server:v1@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
			KonnectivityAgentImage:  "example/agent:v1@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
		},
	}
	for _, key := range []string{
		"CALICO_CNI_IMAGE",
		"CALICO_KUBE_CONTROLLERS_IMAGE",
		"CALICO_NODE_IMAGE",
		"KUBE_PROXY_IMAGE",
		"KONNECTIVITY_AGENT_IMAGE",
		"CNPG_CONTROLLER_IMAGE",
		"POSTGRES_IMAGE",
		"VERIFY_IMAGE",
	} {
		foundation.Cache.ImageArchives = append(foundation.Cache.ImageArchives, FoundationArchive{
			Key:       key,
			Path:      "images/" + strings.ToLower(key) + ".tar",
			SHA256:    strings.Repeat("b", 64),
			Reference: "example/" + strings.ToLower(key) + ":v1@sha256:" + strings.Repeat("c", 64),
			Tagged:    "docker.io/example/" + strings.ToLower(key) + ":v1",
			Worker:    true,
		})
	}
	return foundation
}

func foundationConfigMap(t *testing.T, foundation Foundation) *corev1.ConfigMap {
	t.Helper()
	encoded, err := json.Marshal(foundation)
	if err != nil {
		t.Fatal(err)
	}

	var immutable map[string]any
	if err := json.Unmarshal(encoded, &immutable); err != nil {
		t.Fatal(err)
	}
	delete(immutable, "mutationEnabled")
	canonical, err := json.Marshal(immutable)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(canonical)
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: defaultFoundationName, Namespace: defaultFoundationNamespace},
		Data: map[string]string{
			"foundation.json":   string(encoded),
			"foundation.sha256": hex.EncodeToString(digest[:]),
		},
	}
}

func endpointConfigMap(t *testing.T, foundation Foundation, tenant *tenancyv1alpha1.Tenant, specHash string) *corev1.ConfigMap {
	t.Helper()
	state := newAllocationState(foundation)
	address, err := claimLowestFree(state, foundation, tenant, specHash)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := encodeAllocationState(state)
	if err != nil {
		t.Fatal(err)
	}
	tenant.Status.Endpoint = foundation.Endpoint(address)
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: allocationConfigMapName, Namespace: defaultFoundationNamespace},
		Data:       map[string]string{"allocations.json": encoded},
	}
}

func TestLoadFoundationUsesLiveDockerIdentity(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	foundation := testFoundation()
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(foundationConfigMap(t, foundation)).Build()
	docker := &fakeDockerClient{
		container: DockerContainer{
			ID:       foundation.ManagementContainerID,
			State:    "running",
			Labels:   foundation.ManagementLabels,
			Networks: map[string]string{"kind": foundation.NetworkID},
		},
		network:    DockerNetwork{ID: foundation.NetworkID, Subnets: []string{foundation.Subnet}},
		execResult: DockerExecResult{Output: "{\"generation\":\"generation\",\"schema\":1}\n"},
	}
	observed, err := loadFoundation(context.Background(), kubernetes, docker, defaultFoundationNamespace, defaultFoundationName, "1.36.4", foundation.ControllerImage)
	if err != nil {
		t.Fatal(err)
	}
	if observed.Hash == "" || observed.NetworkID != foundation.NetworkID {
		t.Fatalf("unexpected foundation: %#v", observed)
	}
	docker.network.Subnets = []string{"172.19.0.0/16"}
	if _, err := loadFoundation(context.Background(), kubernetes, docker, defaultFoundationNamespace, defaultFoundationName, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("network identity drift was accepted")
	}
}

func TestFoundationSafetyReadUsesUncachedReader(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	foundation := testFoundation()
	good := foundationConfigMap(t, foundation)
	stale := good.DeepCopy()
	stale.Data["foundation.sha256"] = strings.Repeat("0", 64)
	cached := fake.NewClientBuilder().WithScheme(scheme).WithObjects(stale).Build()
	direct := fake.NewClientBuilder().WithScheme(scheme).WithObjects(good).Build()
	docker := &fakeDockerClient{
		container: DockerContainer{
			ID:       foundation.ManagementContainerID,
			State:    "running",
			Labels:   foundation.ManagementLabels,
			Networks: map[string]string{"kind": foundation.NetworkID},
		},
		network:    DockerNetwork{ID: foundation.NetworkID, Subnets: []string{foundation.Subnet}},
		execResult: DockerExecResult{Output: "{\"generation\":\"generation\",\"schema\":1}\n"},
	}
	reconciler := &TenantReconciler{Client: cached, APIReader: direct, Docker: docker}
	if _, err := loadFoundation(context.Background(), reconciler.reader(), docker, defaultFoundationNamespace, defaultFoundationName, "1.36.4", foundation.ControllerImage); err != nil {
		t.Fatal(err)
	}
}

func TestFoundationRejectsMalformedCacheAndCIDRs(t *testing.T) {
	foundation := testFoundation()
	foundation.Cache.ImageArchives[0].Path = "../escape"
	if err := validateFoundation(foundation, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("unsafe cache path was accepted")
	}
	foundation = testFoundation()
	foundation.ReservedCIDRs = []string{"10.0.0.0/16", "10.0.0.0/16"}
	if err := validateFoundation(foundation, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("duplicate reserved CIDR was accepted")
	}
	foundation = testFoundation()
	foundation.ReservedCIDRs = []string{"2001:db8::/64"}
	if err := validateFoundation(foundation, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("IPv6 reserved CIDR was accepted")
	}
	foundation = testFoundation()
	foundation.Cache.ImageArchives = foundation.Cache.ImageArchives[1:]
	if err := validateFoundation(foundation, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("incomplete worker image inventory was accepted")
	}
	foundation = testFoundation()
	foundation.Cache.ImageArchives[0].Worker = false
	if err := validateFoundation(foundation, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("required worker image with a false worker flag was accepted")
	}
	foundation = testFoundation()
	foundation.Versions["CAPI_VERSION"] = "v9.9.9"
	if err := validateFoundation(foundation, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("wrong pinned provider version was accepted")
	}
	foundation = testFoundation()
	if err := validateFoundation(foundation, "1.36.4", "foreign-controller:image"); err == nil {
		t.Fatal("wrong controller image identity was accepted")
	}
}

func TestLoadFoundationRejectsActiveCacheAndRegistryDrift(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	foundation := testFoundation()
	foundation.OfflineEnforced = true
	foundation.Registry = &FoundationRegistry{
		Address:    "172.18.0.10",
		Port:       5000,
		Generation: "registry-generation",
		Identifier: "registry-id",
	}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(foundationConfigMap(t, foundation)).Build()
	management := DockerContainer{
		ID:               foundation.ManagementContainerID,
		State:            "running",
		Labels:           foundation.ManagementLabels,
		Networks:         map[string]string{"kind": foundation.NetworkID},
		NetworkAddresses: map[string]string{foundation.NetworkID: "172.18.0.2"},
	}
	registry := DockerContainer{
		ID:    foundation.Registry.Identifier,
		State: "running",
		Labels: map[string]string{
			foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
			"cnpg-vcluster.capi/role":        "offline-registry",
			"cnpg-vcluster.capi/generation":  foundation.Registry.Generation,
		},
		Networks:         map[string]string{"kind": foundation.NetworkID},
		NetworkAddresses: map[string]string{foundation.NetworkID: foundation.Registry.Address},
	}
	docker := &fakeDockerClient{
		containers: map[string]DockerContainer{
			management.ID: management,
			registry.ID:   registry,
		},
		network:    DockerNetwork{ID: foundation.NetworkID, Subnets: []string{foundation.Subnet}},
		execResult: DockerExecResult{Output: "{\"generation\":\"wrong\",\"schema\":1}\n"},
	}
	if _, err := loadFoundation(context.Background(), kubernetes, docker, defaultFoundationNamespace, defaultFoundationName, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("changed active cache generation was accepted")
	}
	docker.execResult.Output = "{\"generation\":\"generation\",\"schema\":1}\n"
	registry.NetworkAddresses[foundation.NetworkID] = "172.18.0.11"
	docker.containers[registry.ID] = registry
	if _, err := loadFoundation(context.Background(), kubernetes, docker, defaultFoundationNamespace, defaultFoundationName, "1.36.4", foundation.ControllerImage); err == nil {
		t.Fatal("changed registry address was accepted")
	}
}

func TestEndpointAllocationIsStableReservedAndCASBacked(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).Build()
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	foundation.ReservedCIDRs = append(foundation.ReservedCIDRs, foundation.PoolStart+"/32")
	tenant := &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "uid-a"}}
	first, err := allocateEndpoint(context.Background(), kubernetes, kubernetes, defaultFoundationNamespace, foundation, tenant, "spec-hash")
	if err != nil {
		t.Fatal(err)
	}
	second, err := allocateEndpoint(context.Background(), kubernetes, kubernetes, defaultFoundationNamespace, foundation, tenant, "spec-hash")
	if err != nil {
		t.Fatal(err)
	}
	if first != "172.18.255.2:6443" || second != first {
		t.Fatalf("unexpected stable endpoint: %s %s", first, second)
	}
	other := &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-b", UID: "uid-b"}}
	third, err := allocateEndpoint(context.Background(), kubernetes, kubernetes, defaultFoundationNamespace, foundation, other, "other-hash")
	if err != nil {
		t.Fatal(err)
	}
	if third != "172.18.255.3:6443" {
		t.Fatalf("unexpected second allocation: %s", third)
	}
	if _, err := allocateEndpoint(context.Background(), kubernetes, kubernetes, defaultFoundationNamespace, foundation, &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-c", UID: "uid-c"}}, "hash"); err == nil {
		t.Fatal("exhausted endpoint pool was accepted")
	}
}

func TestEndpointAllocationRejectsMalformedOrChangedState(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	configMap := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: allocationConfigMapName, Namespace: defaultFoundationNamespace},
		Data:       map[string]string{"allocations.json": `{"schema":1,"foundationHash":"old","networkId":"foreign","poolStart":"172.18.255.1","poolEnd":"172.18.255.3","allocations":{"172.18.255.1":{"tenantName":"other","tenantUID":"other-uid","specHash":"other-spec","foundationHash":"old"}}}`},
	}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(configMap).Build()
	tenant := &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "uid-a"}}
	if _, err := allocateEndpoint(context.Background(), kubernetes, kubernetes, defaultFoundationNamespace, foundation, tenant, "hash"); err == nil {
		t.Fatal("changed network identity was accepted")
	}
}

func TestEmptyEndpointStateRebindsToCurrentFoundation(t *testing.T) {
	foundation := testFoundation()
	foundation.Hash = "current"
	state, err := decodeAllocationState(
		`{"schema":1,"foundationHash":"old","networkId":"old-network","poolStart":"192.0.2.1","poolEnd":"192.0.2.2","allocations":{}}`,
		foundation,
	)
	if err != nil {
		t.Fatal(err)
	}
	if state.FoundationHash != foundation.Hash || state.NetworkID != foundation.NetworkID ||
		state.PoolStart != foundation.PoolStart || state.PoolEnd != foundation.PoolEnd {
		t.Fatalf("empty allocation state was not rebound: %#v", state)
	}
}

func TestPeerNetworksRemainReservedForDeletingAndFailedTenants(t *testing.T) {
	scheme := testScheme(t)
	deleting := metav1.Now()
	peer := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-b", UID: "uid-b", DeletionTimestamp: &deleting, Finalizers: []string{tenantFinalizer}},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
		Status: tenancyv1alpha1.TenantStatus{Phase: tenancyv1alpha1.PhaseFailed},
	}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(peer).Build()
	tenant := &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "uid-a"}}
	canonical := validation.CanonicalSpec{PodCIDR: "10.20.1.0/24", ServiceCIDR: "10.30.0.0/16"}
	if err := validatePeerNetworks(context.Background(), kubernetes, tenant, canonical, testFoundation(), "1.36.4"); err == nil {
		t.Fatal("deleting peer network reservation was ignored")
	}
}

func TestPeerNetworksRejectManagementSubnetOverlap(t *testing.T) {
	scheme := testScheme(t)
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).Build()
	tenant := &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "uid-a"}}
	for _, canonical := range []validation.CanonicalSpec{
		{PodCIDR: "172.18.1.0/24", ServiceCIDR: "10.30.0.0/16"},
		{PodCIDR: "10.20.0.0/16", ServiceCIDR: "172.18.2.0/24"},
	} {
		if err := validatePeerNetworks(context.Background(), kubernetes, tenant, canonical, testFoundation(), "1.36.4"); err == nil {
			t.Fatal("management Docker subnet overlap was accepted")
		}
	}
}

func TestVolumeRefusesForeignIdentity(t *testing.T) {
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	name := foundation.Inputs.LabPrefix + "-tenant-a-storage"
	docker := &fakeDockerClient{volumes: map[string]DockerVolume{
		name: {Name: name, CreatedAt: "now", Mountpoint: "/volume", Labels: map[string]string{"foreign": "true"}},
	}}
	reconciler := &TenantReconciler{Docker: docker}
	tenant := &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "uid-a"}}
	if _, err := reconciler.ensureVolume(context.Background(), tenant, "spec-hash", foundation); err == nil {
		t.Fatal("foreign same-name Docker volume was adopted")
	}
}

func TestDockerErrorsArePropagated(t *testing.T) {
	expected := errors.New("docker unavailable")
	reconciler := &TenantReconciler{Docker: &fakeDockerClient{err: expected}}
	_, err := reconciler.ensureVolume(context.Background(), &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Name: "tenant-a"}}, "spec", testFoundation())
	if !errors.Is(err, expected) {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestWorkerPreparationVerifiesCacheMirrorEgressAndPull(t *testing.T) {
	foundation := testFoundation()
	foundation.OfflineEnforced = true
	foundation.Registry = &FoundationRegistry{Address: "172.18.0.10", Port: 5000, Generation: "registry", Identifier: "registry-id"}
	checksum := foundation.Cache.ImageArchives[0].SHA256
	docker := &fakeDockerClient{}
	docker.execFunc = func(command []string) (DockerExecResult, error) {
		joined := strings.Join(command, " ")
		switch {
		case strings.Contains(joined, "sha256sum "):
			return DockerExecResult{Output: checksum + "  archive\n"}, nil
		case strings.Contains(joined, "images inspect"):
			return DockerExecResult{Output: "└── target@sha256:" + strings.Repeat("c", 64)}, nil
		case strings.Contains(joined, "iptables -Z CAPI_OFFLINE"):
			return DockerExecResult{Output: "1 1\n"}, nil
		default:
			return DockerExecResult{}, nil
		}
	}
	reconciler := &TenantReconciler{Docker: docker}
	evidence := workerEvidence(nil, DockerContainer{ID: "worker-id", Name: "worker"}, foundation.Cache.Generation)
	for attempts := 0; !evidence.Prepared && attempts < 20; attempts++ {
		var err error
		evidence, err = reconciler.prepareWorkerStep(context.Background(), DockerContainer{ID: "worker-id", Name: "worker"}, foundation, evidence)
		if err != nil {
			t.Fatal(err)
		}
	}
	if !evidence.Prepared || len(evidence.ImportedImages) != 8 ||
		!evidence.MirrorsConfigured || !evidence.EgressVerified || !evidence.MirrorPullVerified {
		t.Fatalf("worker evidence is incomplete: %#v", evidence)
	}
	joined := make([]string, 0, len(docker.commands))
	for _, command := range docker.commands {
		joined = append(joined, strings.Join(command, " "))
	}
	all := strings.Join(joined, "\n")
	for _, expected := range []string{"images import --digests", "hosts.toml", "CAPI_OFFLINE", "crictl pull"} {
		if !strings.Contains(all, expected) {
			t.Fatalf("worker preparation omitted %q:\n%s", expected, all)
		}
	}
}

func TestWorkerPreparationStopsOnCacheMismatch(t *testing.T) {
	foundation := testFoundation()
	docker := &fakeDockerClient{execResult: DockerExecResult{Output: strings.Repeat("0", 64) + "  archive\n"}}
	reconciler := &TenantReconciler{Docker: docker}
	evidence := workerEvidence(nil, DockerContainer{ID: "worker-id"}, foundation.Cache.Generation)
	if _, err := reconciler.prepareWorkerStep(context.Background(), DockerContainer{ID: "worker-id"}, foundation, evidence); err == nil {
		t.Fatal("cache checksum mismatch was accepted")
	}
	if len(docker.commands) != 1 {
		t.Fatalf("worker mutation continued after checksum mismatch: %#v", docker.commands)
	}
}

func TestWorkerPreparationRejectsWrongImportedDigest(t *testing.T) {
	foundation := testFoundation()
	checksum := foundation.Cache.ImageArchives[0].SHA256
	docker := &fakeDockerClient{execFunc: func(command []string) (DockerExecResult, error) {
		joined := strings.Join(command, " ")
		if strings.Contains(joined, "sha256sum ") {
			return DockerExecResult{Output: checksum + "  archive\n"}, nil
		}
		if strings.Contains(joined, "images inspect") {
			return DockerExecResult{Output: "└── target@sha256:" + strings.Repeat("0", 64)}, nil
		}
		return DockerExecResult{}, nil
	}}
	reconciler := &TenantReconciler{Docker: docker}
	evidence := workerEvidence(nil, DockerContainer{ID: "worker-id"}, foundation.Cache.Generation)
	if _, err := reconciler.prepareWorkerStep(context.Background(), DockerContainer{ID: "worker-id"}, foundation, evidence); err == nil {
		t.Fatal("wrong imported image digest was accepted")
	}
}

func TestCaptureShellExitSurvivesErrexit(t *testing.T) {
	script := captureShellExit("false") + "; printf '%s\\n' \"$rc\""
	output, err := exec.Command("sh", "-ec", script).CombinedOutput()
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(output)) != "1" {
		t.Fatalf("unexpected captured status: %q", output)
	}
}

func TestWorkerPreparationPropagatesCancellationAndBoundsCommands(t *testing.T) {
	foundation := testFoundation()
	docker := &fakeDockerClient{err: context.Canceled}
	reconciler := &TenantReconciler{Docker: docker}
	evidence := workerEvidence(nil, DockerContainer{ID: "worker-id"}, foundation.Cache.Generation)
	if _, err := reconciler.prepareWorkerStep(context.Background(), DockerContainer{ID: "worker-id"}, foundation, evidence); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation was not propagated: %v", err)
	}
	docker.err = nil
	docker.execResult = DockerExecResult{Output: foundation.Cache.ImageArchives[0].SHA256 + "  archive\n"}
	_, _ = reconciler.prepareWorkerStep(context.Background(), DockerContainer{ID: "worker-id"}, foundation, evidence)
	for _, command := range docker.commands[1:] {
		if len(command) < 2 || command[0] != "timeout" || command[1] != "90" {
			t.Fatalf("container command is not bounded: %#v", command)
		}
	}
}

func TestControllerSerializesTenantReconciles(t *testing.T) {
	if controllerOptions().MaxConcurrentReconciles != 1 {
		t.Fatalf("unexpected reconcile concurrency: %d", controllerOptions().MaxConcurrentReconciles)
	}
	status := &tenancyv1alpha1.TenantStatus{}
	tenant := &tenancyv1alpha1.Tenant{ObjectMeta: metav1.ObjectMeta{Generation: 4}}
	setCondition(status, tenant, "Ready", metav1.ConditionFalse, "Pending", "pending")
	if len(status.Conditions) != 1 || status.Conditions[0].ObservedGeneration != 4 ||
		status.Conditions[0].LastTransitionTime.IsZero() {
		t.Fatalf("invalid condition publication: %#v", status.Conditions)
	}
}

func TestDependencyWatchRequeuesMarkedTenant(t *testing.T) {
	object := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "dependency",
		Annotations: map[string]string{
			"tenancy.cnpg-vcluster.io/tenant": "tenant-a",
		},
	}}
	requests := requestsForTenantObject(context.Background(), object)
	if len(requests) != 1 || requests[0].Name != "tenant-a" {
		t.Fatalf("unexpected dependency requeue: %#v", requests)
	}
	object.Annotations = nil
	if requests := requestsForTenantObject(context.Background(), object); len(requests) != 0 {
		t.Fatalf("unmarked object requeued Tenant: %#v", requests)
	}
}

func TestFinalizerUpdatesUseMainTenantResource(t *testing.T) {
	marker := "// +kubebuilder:rbac:groups=tenancy.cnpg-vcluster.io,resources=tenants,verbs=get;list;watch;update"
	source, err := os.ReadFile("tenant_controller.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(source), marker) {
		t.Fatal("Tenant update RBAC required for finalizer writes is missing")
	}
}

func TestRecordedRootReplacementIsRejected(t *testing.T) {
	scheme := testScheme(t)
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{Name: "tenant-a", UID: "tenant-uid"},
		Status: tenancyv1alpha1.TenantStatus{ObservedResources: []tenancyv1alpha1.ObservedResourceIdentity{{
			APIVersion: "v1",
			Kind:       "Namespace",
			Name:       "tenant-a",
			UID:        "recorded-uid",
		}}},
	}
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name: "tenant-a",
		UID:  "replacement-uid",
		Labels: map[string]string{
			foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
		},
		Annotations: map[string]string{
			"tenancy.cnpg-vcluster.io/tenant":          tenant.Name,
			"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
			"tenancy.cnpg-vcluster.io/spec-hash":       "spec-hash",
			"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
			"tenancy.cnpg-vcluster.io/resource":        "namespace",
		},
	}}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithObjects(namespace).Build()
	if err := validateRecordedResources(context.Background(), kubernetes, tenant, "spec-hash", foundation); err == nil {
		t.Fatal("same-name replacement root was accepted")
	}
}

func TestPartialFinalizationReleasesEndpointAfterStatusCrashGap(t *testing.T) {
	scheme := testScheme(t)
	now := metav1.Now()
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{
			Name:              "tenant-a",
			UID:               "uid-a",
			Generation:        1,
			Finalizers:        []string{tenantFinalizer},
			DeletionTimestamp: &now,
		},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
	}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(tenant).WithObjects(tenant).Build()
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	if _, err := allocateEndpoint(context.Background(), kubernetes, kubernetes, defaultFoundationNamespace, foundation, tenant, "spec-hash"); err != nil {
		t.Fatal(err)
	}
	reconciler := &TenantReconciler{Client: kubernetes, APIReader: kubernetes, Docker: &fakeDockerClient{volumes: map[string]DockerVolume{}}}
	current := tenant
	for attempts := 0; attempts < 8; attempts++ {
		if _, err := reconciler.finalizePartial(context.Background(), current, "spec-hash", foundation); err != nil {
			t.Fatal(err)
		}
		var refreshed tenancyv1alpha1.Tenant
		err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &refreshed)
		if client.IgnoreNotFound(err) != nil {
			t.Fatal(err)
		}
		if err != nil {
			break
		}
		current = &refreshed
	}
	var allocations corev1.ConfigMap
	if err := kubernetes.Get(context.Background(), client.ObjectKey{Namespace: defaultFoundationNamespace, Name: allocationConfigMapName}, &allocations); err != nil {
		t.Fatal(err)
	}
	state, err := decodeAllocationState(allocations.Data["allocations.json"], foundation)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Allocations) != 0 {
		t.Fatalf("crash-gap endpoint allocation remained: %#v", state.Allocations)
	}
}

func TestPartialFinalizationDeletesUnrecordedExactVolume(t *testing.T) {
	scheme := testScheme(t)
	now := metav1.Now()
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{
			Name:              "tenant-a",
			UID:               "uid-a",
			Generation:        1,
			Finalizers:        []string{tenantFinalizer},
			DeletionTimestamp: &now,
		},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
		Status: tenancyv1alpha1.TenantStatus{
			Stage: tenancyv1alpha1.StageNamespaceCreated,
			Teardown: &tenancyv1alpha1.TeardownStatus{
				Authority: "TenantAPINeverAuthorized",
			},
		},
	}
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{
		Name: "tenant-a",
		UID:  "namespace-uid",
		Labels: map[string]string{
			foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
		},
		Annotations: map[string]string{
			"tenancy.cnpg-vcluster.io/tenant":          tenant.Name,
			"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
			"tenancy.cnpg-vcluster.io/spec-hash":       "spec-hash",
			"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
			"tenancy.cnpg-vcluster.io/resource":        "namespace",
		},
	}}
	allocation := endpointConfigMap(t, foundation, tenant, "spec-hash")
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(tenant).WithObjects(tenant, namespace, allocation).Build()
	name := foundation.Inputs.LabPrefix + "-" + tenant.Name + "-storage"
	labels := map[string]string{
		foundation.Inputs.OwnershipLabel:           foundation.Inputs.LabPrefix,
		"cnpg-vcluster.capi/role":                  "tenant-storage",
		"cnpg-vcluster.capi/tenant":                tenant.Name,
		"tenancy.cnpg-vcluster.io/tenant-uid":      string(tenant.UID),
		"tenancy.cnpg-vcluster.io/spec-hash":       "spec-hash",
		"tenancy.cnpg-vcluster.io/foundation-hash": foundation.Hash,
	}
	docker := &fakeDockerClient{volumes: map[string]DockerVolume{
		name: {Name: name, CreatedAt: "now", Mountpoint: "/volume", Labels: labels},
	}}
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    docker,
	}
	current := tenant
	for attempts := 0; attempts < 8; attempts++ {
		if _, err := reconciler.finalizePartial(context.Background(), current, "spec-hash", foundation); err != nil {
			t.Fatal(err)
		}
		var refreshed tenancyv1alpha1.Tenant
		err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &refreshed)
		if client.IgnoreNotFound(err) != nil {
			t.Fatal(err)
		}
		if err != nil {
			break
		}
		current = &refreshed
	}
	if _, exists := docker.volumes[name]; exists {
		t.Fatal("exact crash-gap Docker volume remained")
	}
}

func TestPartialFinalizationHonorsPersistedLiveCleanupCheckpoint(t *testing.T) {
	scheme := testScheme(t)
	now := metav1.Now()
	tenant := &tenancyv1alpha1.Tenant{
		ObjectMeta: metav1.ObjectMeta{
			Name:              "tenant-a",
			UID:               "uid-a",
			Generation:        2,
			Finalizers:        []string{tenantFinalizer},
			DeletionTimestamp: &now,
		},
		Spec: tenancyv1alpha1.TenantSpec{
			KubernetesVersion: "1.36.4",
			Workers:           1,
			DatabaseCount:     1,
			PodCIDR:           "10.20.0.0/16",
			ServiceCIDR:       "10.21.0.0/16",
		},
		Status: tenancyv1alpha1.TenantStatus{
			Stage: tenancyv1alpha1.StageEndpointReleased,
			Teardown: &tenancyv1alpha1.TeardownStatus{
				Authority: "LiveBootstrapRBACCleanupComplete",
				Phase:     "LiveBootstrapRBACCleanupComplete",
			},
		},
	}
	kubernetes := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(tenant).WithObjects(tenant).Build()
	foundation := testFoundation()
	foundation.Hash = "foundation-hash"
	reconciler := &TenantReconciler{
		Client:    kubernetes,
		APIReader: kubernetes,
		Docker:    &fakeDockerClient{volumes: map[string]DockerVolume{}},
	}
	if _, err := reconciler.finalizePartial(context.Background(), tenant, "spec-hash", foundation); err != nil {
		t.Fatal(err)
	}
	var updated tenancyv1alpha1.Tenant
	err := kubernetes.Get(context.Background(), client.ObjectKey{Name: tenant.Name}, &updated)
	if ignored := client.IgnoreNotFound(err); ignored != nil {
		t.Fatal(ignored)
	}
	if err == nil && containsString(updated.Finalizers, tenantFinalizer) {
		t.Fatal("persisted live cleanup checkpoint did not permit finalizer removal")
	}
}
