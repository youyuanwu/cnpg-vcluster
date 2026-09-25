package controller

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

type foundationProbeClient struct {
	*fakeDockerClient
	containerInspections int
	networkInspections   int
}

func (docker *foundationProbeClient) InspectContainer(ctx context.Context, id string) (DockerContainer, error) {
	docker.containerInspections++
	return docker.fakeDockerClient.InspectContainer(ctx, id)
}

func (docker *foundationProbeClient) InspectNetwork(ctx context.Context, id string) (DockerNetwork, error) {
	docker.networkInspections++
	return docker.fakeDockerClient.InspectNetwork(ctx, id)
}

func foundationCacheFixture(t *testing.T, offline bool) (*TenantReconciler, *foundationProbeClient, Foundation) {
	t.Helper()
	foundation := testFoundation()
	foundation.OfflineEnforced = offline
	docker := &foundationProbeClient{fakeDockerClient: &fakeDockerClient{
		container: DockerContainer{
			ID:       foundation.ManagementContainerID,
			State:    "running",
			Labels:   foundation.ManagementLabels,
			Networks: map[string]string{"kind": foundation.NetworkID},
		},
		network:    DockerNetwork{ID: foundation.NetworkID, Subnets: []string{foundation.Subnet}},
		execResult: DockerExecResult{Output: "{\"generation\":\"generation\",\"schema\":1}\n"},
	}}
	if offline {
		foundation.Registry = &FoundationRegistry{
			Address: "172.18.0.10", Port: 5000, Generation: "registry-generation", Identifier: "registry-id",
		}
		docker.containers = map[string]DockerContainer{
			foundation.Registry.Identifier: {
				ID:    foundation.Registry.Identifier,
				State: "running",
				Labels: map[string]string{
					foundation.Inputs.OwnershipLabel: foundation.Inputs.LabPrefix,
					"cnpg-vcluster.capi/role":        "offline-registry",
					"cnpg-vcluster.capi/generation":  foundation.Registry.Generation,
				},
				NetworkAddresses: map[string]string{foundation.NetworkID: foundation.Registry.Address},
			},
		}
	}
	configMap := foundationConfigMap(t, foundation)
	foundation.Hash = configMap.Data["foundation.sha256"]
	kubernetes := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(configMap).Build()
	return &TenantReconciler{
		Client: kubernetes, APIReader: kubernetes, Docker: docker, SupportedVersion: "1.36.4",
		MutationEnabled: true, ExpectedControllerImage: foundation.ControllerImage,
	}, docker, foundation
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

func TestFoundationCacheAvoidsRepeatedHostProbes(t *testing.T) {
	for _, offline := range []bool{false, true} {
		name := "online"
		if offline {
			name = "offline"
		}
		t.Run(name, func(t *testing.T) {
			reconciler, docker, foundation := foundationCacheFixture(t, offline)
			for range 43 {
				observed, err := reconciler.loadFoundation(context.Background(), foundation.Hash)
				if err != nil || observed.Hash != foundation.Hash {
					t.Fatalf("load current foundation: %+v, %v", observed, err)
				}
			}
			expectedInspections := 1
			if offline {
				expectedInspections++
			}
			if docker.containerInspections != expectedInspections || docker.networkInspections != 1 || len(docker.commands) != 1 {
				t.Fatalf("repeated host probes: containers=%d networks=%d execs=%d", docker.containerInspections, docker.networkInspections, len(docker.commands))
			}
			restarted := &TenantReconciler{
				Client: reconciler.Client, Docker: docker, SupportedVersion: reconciler.SupportedVersion,
				ExpectedControllerImage: reconciler.ExpectedControllerImage,
			}
			if _, err := restarted.loadFoundation(context.Background(), foundation.Hash); err != nil {
				t.Fatal(err)
			}
			if docker.networkInspections != 2 || len(docker.commands) != 2 {
				t.Fatal("a fresh reconciler reused another reconciler's host validation")
			}
		})
	}
}

func TestFoundationCacheAlwaysReadsCurrentConfigMap(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*Foundation, *corev1.ConfigMap)
		want   string
	}{
		{"checksum", func(_ *Foundation, configMap *corev1.ConfigMap) {
			configMap.Data["foundation.sha256"] = strings.Repeat("0", 64)
		}, "Tenant foundation checksum mismatch"},
		{"immutable hash", func(foundation *Foundation, _ *corev1.ConfigMap) {
			foundation.PoolEnd = "172.18.255.4"
		}, errFoundationMismatch.Error()},
		{"controller image", func(foundation *Foundation, _ *corev1.ConfigMap) {
			foundation.ControllerImage = "other:image"
		}, "Tenant foundation controller image mismatch"},
		{"mutation disabled", func(foundation *Foundation, _ *corev1.ConfigMap) {
			foundation.MutationEnabled = false
		}, ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			reconciler, docker, foundation := foundationCacheFixture(t, false)
			if _, err := reconciler.loadFoundation(context.Background(), foundation.Hash); err != nil {
				t.Fatal(err)
			}
			oldHash := foundation.Hash
			configMap := foundationConfigMap(t, foundation)
			test.change(&foundation, configMap)
			if test.name != "checksum" {
				configMap = foundationConfigMap(t, foundation)
			}
			replaceFoundationConfigMap(t, reconciler.Client, configMap)
			observed, err := reconciler.loadFoundation(context.Background(), oldHash)
			if test.want == "" {
				if err != nil || observed.MutationEnabled || observed.Hash != oldHash {
					t.Fatalf("mutable foundation fields were cached: %+v, %v", observed, err)
				}
			} else if err == nil || err.Error() != test.want {
				t.Fatalf("unexpected foundation error: %v, want %s", err, test.want)
			}
			if test.name == "immutable hash" && !errors.Is(err, errFoundationMismatch) {
				t.Fatalf("foundation mismatch classification was lost: %v", err)
			}
			if docker.containerInspections != 1 || docker.networkInspections != 1 || len(docker.commands) != 1 {
				t.Fatal("invalid or unchanged foundation triggered unnecessary host probes")
			}
		})
	}
}

func TestFoundationCacheExpiresAndDoesNotCacheHostFailures(t *testing.T) {
	_, docker, foundation := foundationCacheFixture(t, true)
	var cache foundationHostValidation
	now := time.Unix(100, 0)
	if err := cache.check(context.Background(), docker, foundation, now); err != nil {
		t.Fatal(err)
	}
	registry := docker.containers[foundation.Registry.Identifier]
	registry.NetworkAddresses[foundation.NetworkID] = "172.18.0.11"
	if err := cache.check(context.Background(), docker, foundation, now.Add(foundationHostCheckPeriod-time.Second)); err != nil {
		t.Fatal(err)
	}
	if len(docker.commands) != 1 {
		t.Fatal("cached host validation was not reused")
	}
	expired := now.Add(foundationHostCheckPeriod)
	for range 2 {
		if err := cache.check(context.Background(), docker, foundation, expired); err == nil || err.Error() != "offline registry network address mismatch" {
			t.Fatalf("expired host check accepted registry drift: %v", err)
		}
	}
	if len(docker.commands) != 3 {
		t.Fatal("failed host validation was cached")
	}
	registry.NetworkAddresses[foundation.NetworkID] = foundation.Registry.Address
	if err := cache.check(context.Background(), docker, foundation, expired); err != nil {
		t.Fatal(err)
	}
	if err := cache.check(context.Background(), docker, foundation, expired.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if len(docker.commands) != 4 {
		t.Fatal("repaired host validation was not cached")
	}
}

func TestFoundationCacheRevalidatesChangedHashImmediately(t *testing.T) {
	reconciler, docker, foundation := foundationCacheFixture(t, false)
	if _, err := reconciler.loadFoundation(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	foundation.PoolEnd = "172.18.255.4"
	replaceFoundationConfigMap(t, reconciler.Client, foundationConfigMap(t, foundation))
	docker.network.Subnets = []string{"172.19.0.0/16"}
	if _, err := reconciler.loadFoundation(context.Background(), ""); err == nil || err.Error() != "management network identity or subnet mismatch" {
		t.Fatalf("changed foundation reused stale host validation: %v", err)
	}
	if docker.containerInspections != 2 || docker.networkInspections != 2 {
		t.Fatal("changed foundation did not revalidate the host")
	}
}

func TestFoundationCacheSerializesConcurrentHostChecks(t *testing.T) {
	_, docker, foundation := foundationCacheFixture(t, true)
	var cache foundationHostValidation
	var wait sync.WaitGroup
	now := time.Unix(100, 0)
	for range 20 {
		wait.Go(func() {
			if err := cache.check(context.Background(), docker, foundation, now); err != nil {
				t.Error(err)
			}
		})
	}
	wait.Wait()
	if docker.containerInspections != 2 || docker.networkInspections != 1 || len(docker.commands) != 1 {
		t.Fatal("concurrent loads repeated successful host probes")
	}
}

func TestFoundationCacheUsesDirectReaderAndPreservesMismatchStatus(t *testing.T) {
	reconciler, docker, foundation := foundationCacheFixture(t, false)
	stale := foundationConfigMap(t, foundation)
	stale.Data["foundation.sha256"] = "stale"
	reconciler.Client = fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(stale).Build()
	if _, err := reconciler.loadFoundation(context.Background(), foundation.Hash); err != nil {
		t.Fatalf("loader did not use the direct reader: %v", err)
	}
	tenant := validTenant("tenant-a")
	tenant.Finalizers = []string{tenantFinalizer}
	tenant.Status.FoundationHash = "old-foundation"
	kubernetes := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(tenant).
		WithObjects(tenant, foundationConfigMap(t, foundation)).
		Build()
	reconciler.Client = kubernetes
	reconciler.APIReader = kubernetes
	docker.err = errors.New("host unavailable")
	if _, err := reconciler.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Name: tenant.Name}}); err == nil || err.Error() != errFoundationMismatch.Error() {
		t.Fatalf("foundation mismatch was obscured by a host failure: %v", err)
	}
	current := &tenancyv1alpha1.Tenant{}
	if err := kubernetes.Get(context.Background(), client.ObjectKeyFromObject(tenant), current); err != nil {
		t.Fatal(err)
	}
	condition := meta.FindStatusCondition(current.Status.Conditions, "FoundationReady")
	if current.Status.Phase != tenancyv1alpha1.PhaseFailed || condition == nil || condition.Reason != "FoundationMismatch" {
		t.Fatalf("foundation mismatch status changed: %+v", current.Status)
	}
	if docker.containerInspections != 1 {
		t.Fatal("mismatch handling probed the host")
	}
}
