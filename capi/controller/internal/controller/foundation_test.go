package controller

import (
	"context"
	"errors"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

// The nil embedded interface makes any Docker call panic.
type forbiddenFoundationDockerClient struct{ DockerClient }

func foundationTestReconciler(t *testing.T, foundation Foundation) *TenantReconciler {
	t.Helper()
	kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).
		WithObjects(foundationConfigMap(t, foundation)).Build()
	return &TenantReconciler{
		Client: kubernetes, APIReader: kubernetes, Docker: forbiddenFoundationDockerClient{},
		SupportedVersion: "1.36.4", MutationEnabled: true, ExpectedControllerImage: foundation.ControllerImage,
	}
}

func replaceFoundationConfigMap(t *testing.T, kubernetes client.Client, desired *corev1.ConfigMap) {
	t.Helper()
	current := &corev1.ConfigMap{}
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(desired), current); err != nil {
		t.Fatal(err)
	}
	current.Data = desired.Data
	if err := kubernetes.Update(context.Background(), current); err != nil {
		t.Fatal(err)
	}
}

func TestLoadFoundationMakesNoDockerCalls(t *testing.T) {
	for _, offline := range []bool{false, true} {
		name := "online"
		if offline {
			name = "offline"
		}
		t.Run(name, func(t *testing.T) {
			foundation := testFoundation()
			foundation.OfflineEnforced = offline
			foundation.Registry = &FoundationRegistry{
				Address: "172.18.0.10", Port: 5000, Generation: "registry-generation", Identifier: "registry-id",
			}
			reconciler := foundationTestReconciler(t, foundation)
			var hash string
			for range 3 {
				observed, err := reconciler.loadFoundation(context.Background(), hash)
				if err != nil || observed.Hash == "" || observed.NetworkID != foundation.NetworkID {
					t.Fatalf("load foundation without host access: %+v, %v", observed, err)
				}
				hash = observed.Hash
			}
			restarted := foundationTestReconciler(t, foundation)
			if _, err := restarted.loadFoundation(context.Background(), hash); err != nil {
				t.Fatalf("load foundation after restart: %v", err)
			}
		})
	}
}

func TestLoadFoundationAcceptsProducerMetadata(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*Foundation)
	}{
		{"producer payload", func(*Foundation) {}},
		{"different tool versions", func(f *Foundation) {
			for key := range f.Versions {
				f.Versions[key] = "installer-owned"
			}
			f.Versions["NEW_TOOL_VERSION"] = "future"
		}},
		{"no tool versions", func(f *Foundation) { f.Versions = nil }},
		{"no cache state digests", func(f *Foundation) {
			f.Cache.StateSHA256, f.Cache.ActiveSHA256 = "", ""
		}},
		{"opaque cache state digests", func(f *Foundation) {
			f.Cache.StateSHA256, f.Cache.ActiveSHA256 = "installer-state", "installer-pointer"
		}},
		{"bootstrap-only registry", func(f *Foundation) {
			f.OfflineEnforced = true
			f.Registry = &FoundationRegistry{Address: "172.18.0.10", Port: 5000}
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			foundation := testFoundation()
			test.change(&foundation)
			reconciler := foundationTestReconciler(t, foundation)
			observed, err := reconciler.loadFoundation(context.Background(), "")
			if err != nil {
				t.Fatalf("installer-owned metadata blocked reconciliation: %v", err)
			}
			if observed.Cache.StateSHA256 != foundation.Cache.StateSHA256 ||
				observed.Cache.ActiveSHA256 != foundation.Cache.ActiveSHA256 ||
				observed.Versions["GO_VERSION"] != foundation.Versions["GO_VERSION"] {
				t.Fatal("producer metadata did not survive decoding")
			}
		})
	}
}

func TestLoadFoundationAlwaysReadsCurrentConfigMap(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*Foundation, *corev1.ConfigMap)
		want   string
	}{
		{"checksum", func(_ *Foundation, cm *corev1.ConfigMap) {
			cm.Data["foundation.sha256"] = strings.Repeat("0", 64)
		}, "Tenant foundation checksum mismatch"},
		{"immutable hash", func(f *Foundation, _ *corev1.ConfigMap) {
			f.PoolEnd = "172.18.255.4"
		}, errFoundationMismatch.Error()},
		{"tool metadata hash", func(f *Foundation, _ *corev1.ConfigMap) {
			f.Versions["GO_VERSION"] = "installer-owned"
		}, errFoundationMismatch.Error()},
		{"cache state hash", func(f *Foundation, _ *corev1.ConfigMap) {
			f.Cache.StateSHA256 = "installer-state"
		}, errFoundationMismatch.Error()},
		{"cache pointer hash", func(f *Foundation, _ *corev1.ConfigMap) {
			f.Cache.ActiveSHA256 = "installer-pointer"
		}, errFoundationMismatch.Error()},
		{"controller image", func(f *Foundation, _ *corev1.ConfigMap) {
			f.ControllerImage = "other:image"
		}, "Tenant foundation controller image mismatch"},
		{"mutation disabled", func(f *Foundation, _ *corev1.ConfigMap) {
			f.MutationEnabled = false
		}, ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			foundation := testFoundation()
			reconciler := foundationTestReconciler(t, foundation)
			initial, err := reconciler.loadFoundation(context.Background(), "")
			if err != nil {
				t.Fatal(err)
			}
			configMap := foundationConfigMap(t, foundation)
			test.change(&foundation, configMap)
			if test.name != "checksum" {
				configMap = foundationConfigMap(t, foundation)
			}
			replaceFoundationConfigMap(t, reconciler.Client, configMap)
			observed, err := reconciler.loadFoundation(context.Background(), initial.Hash)
			if test.want == "" {
				if err != nil || observed.MutationEnabled || observed.Hash != initial.Hash {
					t.Fatalf("mutable foundation fields were cached: %+v, %v", observed, err)
				}
			} else if err == nil || err.Error() != test.want {
				t.Fatalf("unexpected foundation error: %v, want %s", err, test.want)
			}
			if test.want == errFoundationMismatch.Error() && !errors.Is(err, errFoundationMismatch) {
				t.Fatalf("foundation mismatch classification was lost: %v", err)
			}
		})
	}
}

func TestFoundationSafetyReadUsesUncachedReader(t *testing.T) {
	foundation := testFoundation()
	reconciler := foundationTestReconciler(t, foundation)
	stale := foundationConfigMap(t, foundation)
	stale.Data["foundation.sha256"] = "stale"
	reconciler.Client = fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(stale).Build()
	if _, err := reconciler.loadFoundation(context.Background(), ""); err != nil {
		t.Fatalf("loader did not use the direct reader: %v", err)
	}
}

func TestReadFoundationProducerChecksumCompatibility(t *testing.T) {
	// Generated with Python's producer algorithm, including otherwise unparsed metadata.
	const payload = `{"schema":2,"versions":{"GO_VERSION":"future"},"cache":{"generation":"generation","stateSHA256":"producer-state","activeSHA256":"producer-active","imageArchives":[]},"installerMetadata":{"revision":3,"enabled":true},"controllerImage":"controller:test","mutationEnabled":true}`
	const checksum = "480caf59d12f44f66b367c01c1cde08c5ed3024131f49116d2dfce6167dea4a3"
	for _, test := range []struct {
		name    string
		payload string
		wantErr bool
	}{
		{"original", payload, false},
		{"formatting", "\n" + strings.ReplaceAll(payload, ",", ",\n") + "\n", false},
		{"mutable image", strings.ReplaceAll(payload, "controller:test", "controller:next"), false},
		{"mutable mode", strings.ReplaceAll(payload, `"mutationEnabled":true`, `"mutationEnabled":false`), false},
		{"unknown metadata", strings.ReplaceAll(payload, `"revision":3`, `"revision":4`), true},
		{"tool metadata", strings.ReplaceAll(payload, "future", "next"), true},
		{"state metadata", strings.ReplaceAll(payload, "producer-state", "other-state"), true},
		{"active metadata", strings.ReplaceAll(payload, "producer-active", "other-active"), true},
	} {
		t.Run(test.name, func(t *testing.T) {
			configMap := foundationConfigMap(t, testFoundation())
			configMap.Data = map[string]string{"foundation.json": test.payload, "foundation.sha256": checksum}
			kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(configMap).Build()
			observed, err := readFoundation(context.Background(), kubernetes, defaultFoundationNamespace, defaultFoundationName)
			if test.wantErr {
				if err == nil || err.Error() != "Tenant foundation checksum mismatch" {
					t.Fatalf("immutable serialized metadata escaped checksum validation: %v", err)
				}
			} else if err != nil || observed.Hash != checksum || observed.Schema != 2 {
				t.Fatalf("producer checksum is incompatible: %+v, %v", observed, err)
			}
		})
	}
}

func TestLoadFoundationRejectsMalformedPayload(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*corev1.ConfigMap)
		want   string
	}{
		{"invalid JSON", func(cm *corev1.ConfigMap) { cm.Data["foundation.json"] = "{" }, "decode Tenant foundation"},
		{"missing JSON", func(cm *corev1.ConfigMap) { delete(cm.Data, "foundation.json") }, "decode Tenant foundation"},
		{"missing checksum", func(cm *corev1.ConfigMap) { delete(cm.Data, "foundation.sha256") }, "checksum mismatch"},
		{"invalid checksum", func(cm *corev1.ConfigMap) { cm.Data["foundation.sha256"] = "bad" }, "checksum mismatch"},
	} {
		t.Run(test.name, func(t *testing.T) {
			foundation := testFoundation()
			reconciler := foundationTestReconciler(t, foundation)
			configMap := foundationConfigMap(t, foundation)
			test.change(configMap)
			replaceFoundationConfigMap(t, reconciler.Client, configMap)
			if _, err := reconciler.loadFoundation(context.Background(), ""); err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("malformed foundation error: %v, want %s", err, test.want)
			}
		})
	}
}

func TestLoadFoundationRejectsInvalidRuntimeInputs(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*Foundation)
	}{
		{"schema", func(f *Foundation) { f.Schema = 1 }},
		{"management identity", func(f *Foundation) { f.ManagementContainerID = "" }},
		{"management labels", func(f *Foundation) { f.ManagementLabels = nil }},
		{"management role", func(f *Foundation) { f.ManagementLabels["io.x-k8s.kind.role"] = "worker" }},
		{"network identity", func(f *Foundation) { f.NetworkID = "" }},
		{"controller image", func(f *Foundation) { f.ControllerImage = "" }},
		{"Kubernetes compatibility", func(f *Foundation) { f.KubernetesVersion = "v1.35.0" }},
		{"subnet host bits", func(f *Foundation) { f.Subnet = "172.18.0.1/16" }},
		{"subnet IPv6", func(f *Foundation) { f.Subnet = "2001:db8::/64" }},
		{"pool start", func(f *Foundation) { f.PoolStart = "172.19.0.1" }},
		{"pool end", func(f *Foundation) { f.PoolEnd = "172.19.0.2" }},
		{"pool order", func(f *Foundation) { f.PoolEnd = "172.18.255.0" }},
		{"duplicate reserved CIDR", func(f *Foundation) { f.ReservedCIDRs = []string{"10.0.0.0/16", "10.0.0.0/16"} }},
		{"reserved IPv6", func(f *Foundation) { f.ReservedCIDRs = []string{"2001:db8::/64"} }},
		{"reserved host bits", func(f *Foundation) { f.ReservedCIDRs = []string{"10.0.0.1/16"} }},
		{"duplicate allowed subnet", func(f *Foundation) { f.AllowedSubnets = []string{f.Subnet, f.Subnet} }},
		{"allowed IPv6", func(f *Foundation) { f.AllowedSubnets = append(f.AllowedSubnets, "2001:db8::/64") }},
		{"missing management subnet", func(f *Foundation) { f.AllowedSubnets = []string{"127.0.0.0/8"} }},
		{"ownership label", func(f *Foundation) { f.Inputs.OwnershipLabel = "" }},
		{"lab prefix", func(f *Foundation) { f.Inputs.LabPrefix = "" }},
		{"API port", func(f *Foundation) { f.Inputs.APIPort = 0 }},
		{"cluster domain", func(f *Foundation) { f.Inputs.ClusterDomain = "" }},
		{"node image", func(f *Foundation) { f.Inputs.NodeImage = "" }},
		{"cache host path", func(f *Foundation) { f.Inputs.CacheHostPath = "" }},
		{"cache container path", func(f *Foundation) { f.Inputs.CacheContainerPath = "" }},
		{"storage container path", func(f *Foundation) { f.Inputs.StorageContainerPath = "" }},
		{"konnectivity server image", func(f *Foundation) { f.Inputs.KonnectivityServerImage = "" }},
		{"konnectivity agent image", func(f *Foundation) { f.Inputs.KonnectivityAgentImage = "" }},
		{"cache generation", func(f *Foundation) { f.Cache.Generation = "" }},
		{"archive key", func(f *Foundation) { f.Cache.ImageArchives[0].Key = "" }},
		{"archive duplicate", func(f *Foundation) { f.Cache.ImageArchives = append(f.Cache.ImageArchives, f.Cache.ImageArchives[0]) }},
		{"archive escape", func(f *Foundation) { f.Cache.ImageArchives[0].Path = "../escape" }},
		{"archive absolute", func(f *Foundation) { f.Cache.ImageArchives[0].Path = "/images/archive.tar" }},
		{"archive unclean", func(f *Foundation) { f.Cache.ImageArchives[0].Path = "images/../archive.tar" }},
		{"archive path", func(f *Foundation) { f.Cache.ImageArchives[0].Path = "" }},
		{"archive SHA length", func(f *Foundation) { f.Cache.ImageArchives[0].SHA256 = "bad" }},
		{"archive SHA encoding", func(f *Foundation) { f.Cache.ImageArchives[0].SHA256 = strings.Repeat("z", 64) }},
		{"archive reference", func(f *Foundation) { f.Cache.ImageArchives[0].Reference = "" }},
		{"archive tag", func(f *Foundation) { f.Cache.ImageArchives[0].Tagged = "" }},
		{"worker archive missing", func(f *Foundation) { f.Cache.ImageArchives = f.Cache.ImageArchives[1:] }},
		{"worker archive marker", func(f *Foundation) { f.Cache.ImageArchives[0].Worker = false }},
		{"registry missing", func(f *Foundation) { f.Registry = nil }},
		{"registry address", func(f *Foundation) { f.Registry.Address = "" }},
		{"registry port", func(f *Foundation) { f.Registry.Port = 0 }},
	} {
		t.Run(test.name, func(t *testing.T) {
			foundation := testFoundation()
			foundation.OfflineEnforced = true
			foundation.Registry = &FoundationRegistry{Address: "172.18.0.10", Port: 5000}
			test.change(&foundation)
			if _, err := foundationTestReconciler(t, foundation).loadFoundation(context.Background(), ""); err == nil {
				t.Fatal("invalid runtime input was accepted")
			}
		})
	}
}

func TestFoundationCutoverCompatibility(t *testing.T) {
	foundation := testFoundation()
	reconciler := foundationTestReconciler(t, foundation)
	initial, err := reconciler.loadFoundation(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	reconciler.ExpectedControllerImage = ""
	if _, err := reconciler.loadFoundation(context.Background(), initial.Hash); err == nil {
		t.Fatal("missing controller image identity was accepted")
	}
	foundation.ControllerImage = "controller:next"
	replaceFoundationConfigMap(t, reconciler.Client, foundationConfigMap(t, foundation))
	reconciler.ExpectedControllerImage = foundation.ControllerImage
	if observed, err := reconciler.loadFoundation(context.Background(), initial.Hash); err != nil || observed.Hash != initial.Hash {
		t.Fatalf("compatible controller-image cutover changed lifecycle identity: %+v, %v", observed, err)
	}
}

func TestFoundationReconcileGatesPreserveLifecycleStatus(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*Foundation, *tenancyv1alpha1.Tenant)
		reason string
	}{
		{"lifecycle mismatch before runtime validation", func(f *Foundation, tenant *tenancyv1alpha1.Tenant) {
			tenant.Status.FoundationHash = "old-foundation"
			f.Schema = 1
		}, "FoundationMismatch"},
		{"mutation disabled", func(f *Foundation, _ *tenancyv1alpha1.Tenant) {
			f.MutationEnabled = false
		}, "FoundationMutationDisabled"},
		{"controller image mismatch", func(f *Foundation, _ *tenancyv1alpha1.Tenant) {
			f.ControllerImage = "controller:next"
		}, "FoundationInvalid"},
	} {
		t.Run(test.name, func(t *testing.T) {
			foundation := testFoundation()
			tenant := validTenant("tenant-a")
			tenant.Status.FoundationHash = foundationConfigMap(t, foundation).Data["foundation.sha256"]
			test.change(&foundation, tenant)
			kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).WithStatusSubresource(tenant).
				WithObjects(tenant, foundationConfigMap(t, foundation)).Build()
			reconciler := &TenantReconciler{
				Client: kubernetes, APIReader: kubernetes, Docker: forbiddenFoundationDockerClient{},
				SupportedVersion: "1.36.4", MutationEnabled: true, ExpectedControllerImage: "controller:test",
			}
			if _, err := reconciler.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Name: tenant.Name}}); err == nil {
				t.Fatal("foundation gate did not block reconciliation")
			}
			current := &tenancyv1alpha1.Tenant{}
			if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(tenant), current); err != nil {
				t.Fatal(err)
			}
			condition := meta.FindStatusCondition(current.Status.Conditions, "Ready")
			if current.Status.Phase != tenancyv1alpha1.PhaseFailed || condition == nil || condition.Reason != test.reason {
				t.Fatalf("foundation failure status changed: %+v", current.Status)
			}
			if test.reason == "FoundationMismatch" {
				condition = meta.FindStatusCondition(current.Status.Conditions, "FoundationReady")
				if condition == nil || condition.Reason != test.reason {
					t.Fatalf("foundation mismatch condition changed: %+v", current.Status)
				}
			}
			if current.Status.FoundationHash != tenant.Status.FoundationHash || len(current.Finalizers) != 0 {
				t.Fatal("foundation gate changed lifecycle identity or added a finalizer")
			}
		})
	}
}

func TestFoundationDeletionSubsetRemainsFailClosed(t *testing.T) {
	subset := Foundation{
		Schema: 2, NetworkID: "network-id", PoolStart: "172.18.255.1", PoolEnd: "172.18.255.3",
		Inputs: FoundationInputs{OwnershipLabel: "example.io/owned", LabPrefix: "example", StorageContainerPath: "/var/lib/storage"},
	}
	for _, test := range []struct {
		name   string
		change func(*Foundation)
	}{
		{"schema", func(f *Foundation) { f.Schema = 1 }},
		{"network", func(f *Foundation) { f.NetworkID = "" }},
		{"pool start", func(f *Foundation) { f.PoolStart = "" }},
		{"pool end", func(f *Foundation) { f.PoolEnd = "" }},
		{"ownership label", func(f *Foundation) { f.Inputs.OwnershipLabel = "" }},
		{"lab prefix", func(f *Foundation) { f.Inputs.LabPrefix = "" }},
		{"storage path", func(f *Foundation) { f.Inputs.StorageContainerPath = "" }},
	} {
		t.Run(test.name, func(t *testing.T) {
			foundation := subset
			test.change(&foundation)
			kubernetes := foundationTestReconciler(t, foundation).Client
			_, err := loadFoundationForDeletion(context.Background(), kubernetes, defaultFoundationNamespace, defaultFoundationName, "")
			if err == nil || err.Error() != "Tenant deletion foundation identity is incomplete" {
				t.Fatalf("incomplete deletion identity accepted: %v", err)
			}
		})
	}
	reconciler := foundationTestReconciler(t, subset)
	observed, err := loadFoundationForDeletion(context.Background(), reconciler.Client, defaultFoundationNamespace, defaultFoundationName, "")
	if err != nil {
		t.Fatalf("deletion required normal reconciliation inputs: %v", err)
	}
	if _, err := loadFoundationForDeletion(context.Background(), reconciler.Client, defaultFoundationNamespace, defaultFoundationName, observed.Hash); err != nil {
		t.Fatalf("matching deletion lifecycle identity rejected: %v", err)
	}
	if _, err := loadFoundationForDeletion(context.Background(), reconciler.Client, defaultFoundationNamespace, defaultFoundationName, "old-foundation"); !errors.Is(err, errFoundationMismatch) {
		t.Fatalf("deletion lifecycle mismatch was not rejected: %v", err)
	}
	configMap := foundationConfigMap(t, subset)
	configMap.Data["foundation.sha256"] = "bad"
	replaceFoundationConfigMap(t, reconciler.Client, configMap)
	if _, err := loadFoundationForDeletion(context.Background(), reconciler.Client, defaultFoundationNamespace, defaultFoundationName, observed.Hash); err == nil || err.Error() != "Tenant foundation checksum mismatch" {
		t.Fatalf("deletion checksum mismatch was not rejected: %v", err)
	}
}
