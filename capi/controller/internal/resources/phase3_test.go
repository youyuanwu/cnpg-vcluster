package resources

import (
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

func TestNetworkBuilderPinsImagesChunksAndReferencesSources(t *testing.T) {
	context := resourceContext()
	calico := []byte(strings.Join([]string{
		"apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: calico-node\n  namespace: kube-system\nspec:\n  template:\n    spec:\n      containers:\n      - name: calico-node\n        image: calico/node:tag\n        env: []\n      initContainers:\n      - name: install-cni\n        image: calico/cni:tag\n      - name: upgrade-ipam\n        image: calico/cni:tag",
		"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: calico-kube-controllers\n  namespace: kube-system\nspec:\n  template:\n    spec:\n      containers:\n      - name: controller\n        image: calico/controllers:tag",
		"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: calico-typha\n  namespace: kube-system\nspec:\n  template:\n    spec:\n      containers:\n      - name: typha\n        image: calico/node:tag",
	}, "\n---\n"))
	bundle, err := BuildNetwork(context, calico, NetworkImages{
		CalicoCNI: "calico/cni:exact", CalicoCNITagged: "calico/cni:tag",
		CalicoNode: "calico/node:exact", CalicoNodeTagged: "calico/node:tag",
		CalicoControllers: "calico/controllers:exact", CalicoControllersTag: "calico/controllers:tag",
		KubeProxy: "kube-proxy:exact", Verify: "verify:exact",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(bundle.Sources) == 0 || len(bundle.Inventory) != len(bundle.Sources) {
		t.Fatalf("unexpected network sources: %#v", bundle)
	}
	references, _, _ := unstructured.NestedSlice(bundle.ResourceSet.Object, "spec", "resources")
	if len(references) != len(bundle.Sources) {
		t.Fatalf("resource references do not match sources: %#v", references)
	}
	if !strings.Contains(bundle.Sources[0].Data["addons.yaml"], context.Spec.PodCIDR) {
		t.Fatal("Calico pool CIDR was not rendered")
	}
}

func TestStorageAndCNPGBuildersPreserveCountsAndAffinity(t *testing.T) {
	context := resourceContext()
	context.Spec.DatabaseCount = 3
	context.Spec.Workers = 1
	storage := StorageObjects(context, "capi-hostpath", "verify:exact")
	if len(storage) != 4 {
		t.Fatalf("unexpected storage object count: %d", len(storage))
	}
	database := CNPGObjects(context, "capi-hostpath", "postgres:exact")
	pvs := 0
	for _, object := range database {
		if object.GetKind() == "PersistentVolume" {
			pvs++
			if _, found, _ := unstructured.NestedFieldNoCopy(object.Object, "spec", "nodeAffinity"); found {
				t.Fatal("CNPG static PV has node affinity")
			}
		}
		if object.GetKind() == "Cluster" {
			affinity, _, _ := unstructured.NestedString(object.Object, "spec", "affinity", "podAntiAffinityType")
			if affinity != "preferred" {
				t.Fatalf("unexpected anti-affinity: %s", affinity)
			}
		}
	}
	if pvs != 3 {
		t.Fatalf("unexpected CNPG PV count: %d", pvs)
	}
}

func TestNetworkSourceRejectsOversizedDocument(t *testing.T) {
	context := resourceContext()
	_, _, err := packageNetworkSources(context, "network", strings.Repeat("x", NetworkSourceLimit))
	if err == nil {
		t.Fatal("oversized single network document was accepted")
	}
}
