package resources

import (
	"fmt"
	"slices"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

func TestNetworkBuilderPinsImagesAndReturnsDirectObjects(t *testing.T) {
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
		KubeProxy: "kube-proxy:exact",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(bundle.Objects) == 0 {
		t.Fatal("network object inventory is empty")
	}
	for _, object := range bundle.Objects {
		annotations := object.GetAnnotations()
		if annotations[TenantUIDAnnotation] != string(context.Tenant.UID) ||
			annotations[ResourceAnnotation] == "" {
			t.Fatalf("network object ownership markers are incomplete: %s/%s", object.GetKind(), object.GetName())
		}
	}
	encoded, err := EncodeDocuments(bundle.Objects)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(encoded, context.Spec.PodCIDR) {
		t.Fatal("Calico pool CIDR was not rendered in direct objects")
	}
}

func TestStorageAndCNPGBuildersPreserveCountsAndAffinity(t *testing.T) {
	context := resourceContext()
	context.Spec.DatabaseCount = 3
	context.Spec.Workers = 1
	storage := StorageObjects(context, "capi-hostpath")
	if len(storage) != 1 {
		t.Fatalf("unexpected storage object count: %d", len(storage))
	}
	database := CNPGObjects(context, "capi-hostpath", "postgres:exact")
	pvs := 0
	clusters := 0
	for _, object := range database {
		if object.GetKind() == "PersistentVolume" {
			pvs++
			if _, found, _ := unstructured.NestedFieldNoCopy(object.Object, "spec", "nodeAffinity"); found {
				t.Fatal("CNPG static PV has node affinity")
			}
			for _, field := range []struct {
				path []string
				want string
			}{
				{[]string{"hostPath", "path"}, fmt.Sprintf("%s/volumes/cnpg/%d", context.Inputs.StorageContainerPath, pvs)},
				{[]string{"hostPath", "type"}, "DirectoryOrCreate"},
				{[]string{"claimRef", "namespace"}, "database"},
				{[]string{"claimRef", "name"}, fmt.Sprintf("capi-postgres-%d", pvs)},
				{[]string{"storageClassName"}, "capi-hostpath"},
				{[]string{"persistentVolumeReclaimPolicy"}, "Retain"},
				{[]string{"capacity", "storage"}, "1Gi"},
			} {
				got, _, _ := unstructured.NestedString(object.Object, append([]string{"spec"}, field.path...)...)
				if got != field.want {
					t.Fatalf("PV %s %v = %q, want %q", object.GetName(), field.path, got, field.want)
				}
			}
			modes, _, _ := unstructured.NestedStringSlice(object.Object, "spec", "accessModes")
			if !slices.Equal(modes, []string{"ReadWriteOnce"}) {
				t.Fatalf("unexpected PV access modes: %v", modes)
			}
		}
		if object.GetKind() == "Cluster" {
			clusters++
			image, _, _ := unstructured.NestedString(object.Object, "spec", "imageName")
			instances, _, _ := unstructured.NestedInt64(object.Object, "spec", "instances")
			if image != "postgres:exact" || instances != 3 {
				t.Fatalf("unexpected CNPG image or instance count: %s %d", image, instances)
			}
			affinity, _, _ := unstructured.NestedString(object.Object, "spec", "affinity", "podAntiAffinityType")
			if affinity != "preferred" {
				t.Fatalf("unexpected anti-affinity: %s", affinity)
			}
		}
	}
	if pvs != 3 || clusters != 1 {
		t.Fatalf("unexpected CNPG object counts: %d PVs, %d Clusters", pvs, clusters)
	}
}

func TestWorkerBootstrapCommandsArePassedToKubeadm(t *testing.T) {
	context := resourceContext()
	context.WorkerBootstrapCommands = []string{
		"ctr --namespace k8s.io images import --digests '/cache/postgres.tar'",
		"mkdir -p '/var/lib/storage/volumes/cnpg/1' && chown 26:26 '/var/lib/storage/volumes/cnpg/1' && chmod 0700 '/var/lib/storage/volumes/cnpg/1'",
	}
	template := KubeadmConfigTemplate(context)
	commands, found, err := unstructured.NestedStringSlice(
		template.Object,
		"spec",
		"template",
		"spec",
		"preKubeadmCommands",
	)
	if err != nil || !found {
		t.Fatalf("worker bootstrap commands are missing: found=%v err=%v", found, err)
	}
	if !slices.Equal(commands, context.WorkerBootstrapCommands) {
		t.Fatalf("unexpected worker bootstrap commands: %#v", commands)
	}
}
