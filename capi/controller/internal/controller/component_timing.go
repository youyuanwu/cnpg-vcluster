package controller

import (
	"context"
	"strings"
	"sync"
	"time"

	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"

	tenancyv1alpha1 "github.com/youyuanwu/cnpg-vcluster/capi/controller/api/v1alpha1"
)

type componentTimingTracker struct {
	mu      sync.Mutex
	current map[string]string
}

var componentTimingOrder = map[string]int{
	"control-plane":            0,
	"worker-image-preparation": 1,
	"network":                  2,
	"worker-readiness":         3,
	"cnpg-operator":            4,
	"database":                 5,
}

func (tracker *componentTimingTracker) transition(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	component string,
) {
	key := componentTimingKey(tenant.Name, tenant.UID)
	tracker.mu.Lock()
	defer tracker.mu.Unlock()
	if tracker.current == nil {
		tracker.current = map[string]string{}
	}
	previous := tracker.current[key]
	if previous == "" &&
		tenantHasReadinessObservation(tenant) {
		return
	}
	if previous == component {
		return
	}
	if previous != "" && componentTimingOrder[component] <= componentTimingOrder[previous] {
		return
	}
	if previous != "" {
		logComponentTransition(ctx, tenant, previous, "ready")
	}
	logComponentTransition(ctx, tenant, component, "waiting")
	tracker.current[key] = component
}

func (tracker *componentTimingTracker) complete(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
) {
	key := componentTimingKey(tenant.Name, tenant.UID)
	tracker.mu.Lock()
	defer tracker.mu.Unlock()
	if component := tracker.current[key]; component != "" {
		logComponentTransition(ctx, tenant, component, "ready")
	}
	delete(tracker.current, key)
}

func (tracker *componentTimingTracker) clearName(name string) {
	tracker.mu.Lock()
	defer tracker.mu.Unlock()
	for key := range tracker.current {
		if strings.HasPrefix(key, name+"/") {
			delete(tracker.current, key)
		}
	}
}

func componentTimingKey(name string, uid types.UID) string {
	return name + "/" + string(uid)
}

func logComponentTransition(
	ctx context.Context,
	tenant *tenancyv1alpha1.Tenant,
	component,
	state string,
) {
	elapsed := time.Duration(0)
	if !tenant.CreationTimestamp.IsZero() {
		elapsed = time.Since(tenant.CreationTimestamp.Time)
		if elapsed < 0 {
			elapsed = 0
		}
	}
	ctrl.LoggerFrom(ctx).Info(
		"Tenant component transition",
		"tenant",
		tenant.Name,
		"tenantUID",
		tenant.UID,
		"component",
		component,
		"state",
		state,
		"elapsedSeconds",
		elapsed.Seconds(),
	)
}
