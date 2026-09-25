package controller

import (
	"context"
	"errors"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

func TestDatabaseStructurallyReadyUsesOnlyClusterHealth(t *testing.T) {
	readError := errors.New("cluster read failed")
	for _, test := range []struct {
		name      string
		missing   bool
		status    map[string]any
		count     int32
		readError error
		ready     bool
	}{
		{name: "absent", missing: true, count: 1},
		{name: "missing status", count: 1},
		{name: "unhealthy", status: map[string]any{"phase": "Creating a new replica", "readyInstances": int64(3)}, count: 3},
		{name: "missing phase", status: map[string]any{"readyInstances": int64(3)}, count: 3},
		{name: "missing count", status: map[string]any{"phase": "Cluster in healthy state"}, count: 3},
		{name: "malformed count", status: map[string]any{"phase": "Cluster in healthy state", "readyInstances": "3"}, count: 3},
		{name: "no ready instances", status: map[string]any{"phase": "Cluster in healthy state", "readyInstances": int64(0)}, count: 3},
		{name: "under ready", status: map[string]any{"phase": "Cluster in healthy state", "readyInstances": int64(2)}, count: 3},
		{name: "over ready", status: map[string]any{"phase": "Cluster in healthy state", "readyInstances": int64(4)}, count: 3},
		{name: "healthy singleton", status: map[string]any{"phase": "Cluster in healthy state", "readyInstances": int64(1)}, count: 1, ready: true},
		{name: "healthy replicas", status: map[string]any{"phase": "Cluster in healthy state", "readyInstances": int64(3)}, count: 3, ready: true},
		{name: "read failure", count: 1, readError: readError},
	} {
		t.Run(test.name, func(t *testing.T) {
			builder := fake.NewClientBuilder()
			gvk := schema.GroupVersionKind{Group: "postgresql.cnpg.io", Version: "v1", Kind: "Cluster"}
			if !test.missing {
				cluster := &unstructured.Unstructured{}
				cluster.SetGroupVersionKind(gvk)
				cluster.SetNamespace("database")
				cluster.SetName("capi-postgres")
				if test.status != nil {
					cluster.Object["status"] = test.status
				}
				builder = builder.WithObjects(cluster)
			}
			reads := 0
			tenantClient := builder.WithInterceptorFuncs(interceptor.Funcs{
				Get: func(ctx context.Context, underlying client.WithWatch, key client.ObjectKey, object client.Object, opts ...client.GetOption) error {
					reads++
					if key != (client.ObjectKey{Namespace: "database", Name: "capi-postgres"}) || object.GetObjectKind().GroupVersionKind() != gvk {
						t.Fatalf("unexpected readiness read: %s %v", key, object.GetObjectKind().GroupVersionKind())
					}
					if test.readError != nil {
						return test.readError
					}
					return underlying.Get(ctx, key, object, opts...)
				},
				List: func(context.Context, client.WithWatch, client.ObjectList, ...client.ListOption) error {
					t.Fatal("database readiness must not enumerate Pods or PVCs")
					return nil
				},
			}).Build()
			ready, err := databaseStructurallyReady(context.Background(), tenantClient, test.count)
			if ready != test.ready || !errors.Is(err, test.readError) {
				t.Fatalf("ready=%t err=%v; want ready=%t err=%v", ready, err, test.ready, test.readError)
			}
			if reads != 1 {
				t.Fatalf("expected one Cluster observation, got %d", reads)
			}
		})
	}
}
