package controller

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"path"
	"sort"
	"strconv"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/youyuanwu/cnpg-vcluster/capi/controller/internal/resources"
)

var errFoundationMismatch = errors.New("Tenant foundation identity changed")

const (
	defaultFoundationNamespace = "tenant-system"
	defaultFoundationName      = "tenant-foundation"
	allocationConfigMapName    = "tenant-endpoint-allocations"
)

var requiredWorkerImageKeys = []string{
	"CALICO_CNI_IMAGE",
	"CALICO_KUBE_CONTROLLERS_IMAGE",
	"CALICO_NODE_IMAGE",
	"KUBE_PROXY_IMAGE",
	"KONNECTIVITY_AGENT_IMAGE",
	"CNPG_CONTROLLER_IMAGE",
	"POSTGRES_IMAGE",
}

type FoundationArchive struct {
	Key       string `json:"key"`
	Path      string `json:"path"`
	SHA256    string `json:"sha256"`
	Reference string `json:"reference"`
	Tagged    string `json:"tagged"`
	Worker    bool   `json:"worker"`
}

type FoundationCache struct {
	Generation    string              `json:"generation"`
	StateSHA256   string              `json:"stateSHA256"`
	ActiveSHA256  string              `json:"activeSHA256"`
	ImageArchives []FoundationArchive `json:"imageArchives"`
}

type FoundationRegistry struct {
	Address    string `json:"address"`
	Port       int32  `json:"port"`
	Generation string `json:"generation"`
	Identifier string `json:"identifier"`
}

type FoundationInputs struct {
	OwnershipLabel          string `json:"ownershipLabel"`
	LabPrefix               string `json:"labPrefix"`
	APIPort                 int32  `json:"apiPort"`
	ClusterDomain           string `json:"clusterDomain"`
	NodeImage               string `json:"nodeImage"`
	CacheHostPath           string `json:"cacheHostPath"`
	CacheContainerPath      string `json:"cacheContainerPath"`
	StorageContainerPath    string `json:"storageContainerPath"`
	KonnectivityServerImage string `json:"konnectivityServerImage"`
	KonnectivityAgentImage  string `json:"konnectivityAgentImage"`
}

type Foundation struct {
	Schema                int                 `json:"schema"`
	ManagementContainerID string              `json:"managementContainerId"`
	ManagementLabels      map[string]string   `json:"managementContainerLabels"`
	NetworkID             string              `json:"networkId"`
	Subnet                string              `json:"subnet"`
	PoolStart             string              `json:"poolStart"`
	PoolEnd               string              `json:"poolEnd"`
	ReservedCIDRs         []string            `json:"reservedCIDRs"`
	AllowedSubnets        []string            `json:"allowedSubnets"`
	KubernetesVersion     string              `json:"kubernetesVersion"`
	ControllerImage       string              `json:"controllerImage"`
	MutationEnabled       bool                `json:"mutationEnabled"`
	OfflineEnforced       bool                `json:"offlineEnforced"`
	Versions              map[string]string   `json:"versions"`
	Cache                 FoundationCache     `json:"cache"`
	Registry              *FoundationRegistry `json:"registry"`
	Inputs                FoundationInputs    `json:"inputs"`
	Hash                  string              `json:"-"`
}

func (reconciler *TenantReconciler) loadFoundation(ctx context.Context, lifecycleHash string) (Foundation, error) {
	foundation, err := readFoundation(ctx, reconciler.reader(), reconciler.foundationNamespace(), reconciler.foundationName())
	if err != nil {
		return Foundation{}, err
	}
	if lifecycleHash != "" && foundation.Hash != lifecycleHash {
		return Foundation{}, errFoundationMismatch
	}
	if err := validateFoundation(foundation, reconciler.SupportedVersion, reconciler.ExpectedControllerImage); err != nil {
		return Foundation{}, err
	}
	return foundation, nil
}

func loadFoundationForDeletion(ctx context.Context, reader client.Reader, namespace, name, lifecycleHash string) (Foundation, error) {
	foundation, err := readFoundation(ctx, reader, namespace, name)
	if err != nil {
		return Foundation{}, err
	}
	if lifecycleHash != "" && foundation.Hash != lifecycleHash {
		return Foundation{}, errFoundationMismatch
	}
	if foundation.Schema != 2 ||
		foundation.NetworkID == "" ||
		foundation.PoolStart == "" ||
		foundation.PoolEnd == "" ||
		foundation.Inputs.OwnershipLabel == "" ||
		foundation.Inputs.LabPrefix == "" ||
		foundation.Inputs.StorageContainerPath == "" {
		return Foundation{}, fmt.Errorf("Tenant deletion foundation identity is incomplete")
	}
	return foundation, nil
}

func readFoundation(ctx context.Context, reader client.Reader, namespace, name string) (Foundation, error) {
	var configMap corev1.ConfigMap
	if err := reader.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &configMap); err != nil {
		return Foundation{}, fmt.Errorf("read Tenant foundation: %w", err)
	}
	encoded := configMap.Data["foundation.json"]
	expectedHash := configMap.Data["foundation.sha256"]
	var immutable map[string]any
	if err := json.Unmarshal([]byte(encoded), &immutable); err != nil {
		return Foundation{}, fmt.Errorf("decode Tenant foundation: %w", err)
	}
	delete(immutable, "mutationEnabled")
	delete(immutable, "controllerImage")
	canonical, err := json.Marshal(immutable)
	if err != nil {
		return Foundation{}, fmt.Errorf("encode immutable Tenant foundation: %w", err)
	}
	actualHash := sha256.Sum256(canonical)
	hash := hex.EncodeToString(actualHash[:])
	if expectedHash == "" || expectedHash != hash {
		return Foundation{}, fmt.Errorf("Tenant foundation checksum mismatch")
	}
	var foundation Foundation
	if err := json.Unmarshal([]byte(encoded), &foundation); err != nil {
		return Foundation{}, fmt.Errorf("decode Tenant foundation: %w", err)
	}
	foundation.Hash = hash
	return foundation, nil
}

func validateFoundation(foundation Foundation, supportedVersion, expectedControllerImage string) error {
	if foundation.Schema != 2 {
		return fmt.Errorf("unsupported Tenant foundation schema")
	}
	if foundation.ManagementContainerID == "" || foundation.NetworkID == "" || foundation.ControllerImage == "" {
		return fmt.Errorf("Tenant foundation identity is incomplete")
	}
	if foundation.ManagementLabels["io.x-k8s.kind.cluster"] == "" ||
		foundation.ManagementLabels["io.x-k8s.kind.role"] != "control-plane" {
		return fmt.Errorf("Tenant foundation management labels are incomplete")
	}
	if strings.TrimPrefix(foundation.KubernetesVersion, "v") != strings.TrimPrefix(supportedVersion, "v") {
		return fmt.Errorf("Tenant foundation Kubernetes version mismatch")
	}
	if expectedControllerImage == "" || foundation.ControllerImage != expectedControllerImage {
		return fmt.Errorf("Tenant foundation controller image mismatch")
	}
	subnet, err := canonicalIPv4Prefix("foundation subnet", foundation.Subnet)
	if err != nil {
		return err
	}
	start, err := netip.ParseAddr(foundation.PoolStart)
	if err != nil || !start.Is4() || !subnet.Contains(start) {
		return fmt.Errorf("foundation endpoint pool start is invalid")
	}
	end, err := netip.ParseAddr(foundation.PoolEnd)
	if err != nil || !end.Is4() || !subnet.Contains(end) || end.Less(start) {
		return fmt.Errorf("foundation endpoint pool end is invalid")
	}
	if err := validateCIDRSet("reserved CIDR", foundation.ReservedCIDRs); err != nil {
		return err
	}
	if err := validateCIDRSet("allowed subnet", foundation.AllowedSubnets); err != nil {
		return err
	}
	if !contains(foundation.AllowedSubnets, foundation.Subnet) {
		return fmt.Errorf("foundation allowed subnets omit the management subnet")
	}
	inputs := foundation.Inputs
	if inputs.OwnershipLabel == "" || inputs.LabPrefix == "" || inputs.APIPort < 1 ||
		inputs.ClusterDomain == "" || inputs.NodeImage == "" || inputs.CacheHostPath == "" ||
		inputs.CacheContainerPath == "" || inputs.StorageContainerPath == "" ||
		inputs.KonnectivityServerImage == "" || inputs.KonnectivityAgentImage == "" {
		return fmt.Errorf("Tenant foundation inputs are incomplete")
	}
	if foundation.Cache.Generation == "" {
		return fmt.Errorf("Tenant foundation cache identity is invalid")
	}
	keys := map[string]struct{}{}
	for _, archive := range foundation.Cache.ImageArchives {
		cleaned := path.Clean(archive.Path)
		if archive.Key == "" || cleaned != archive.Path || strings.HasPrefix(cleaned, "/") ||
			strings.HasPrefix(cleaned, "../") || !validSHA256(archive.SHA256) ||
			archive.Reference == "" || archive.Tagged == "" {
			return fmt.Errorf("Tenant foundation cache archive is invalid")
		}
		if _, duplicate := keys[archive.Key]; duplicate {
			return fmt.Errorf("Tenant foundation cache archive key is duplicated")
		}
		keys[archive.Key] = struct{}{}
	}
	for _, key := range requiredWorkerImageKeys {
		archive, present := archiveByKey(foundation.Cache.ImageArchives, key)
		if !present {
			return fmt.Errorf("Tenant foundation worker image %s is missing", key)
		}
		if !archive.Worker {
			return fmt.Errorf("Tenant foundation worker image %s is not marked for preparation", key)
		}
	}

	if foundation.OfflineEnforced {
		if foundation.Registry == nil || foundation.Registry.Address == "" ||
			foundation.Registry.Port < 1 {
			return fmt.Errorf("offline Tenant foundation registry is incomplete")
		}
	}
	return nil
}

func archiveByKey(archives []FoundationArchive, key string) (FoundationArchive, bool) {
	for _, archive := range archives {
		if archive.Key == key {
			return archive, true
		}
	}
	return FoundationArchive{}, false
}

func canonicalIPv4Prefix(description, value string) (netip.Prefix, error) {
	prefix, err := netip.ParsePrefix(value)
	if err != nil || !prefix.Addr().Is4() || prefix != prefix.Masked() {
		return netip.Prefix{}, fmt.Errorf("%s must be a canonical IPv4 CIDR", description)
	}
	return prefix, nil
}

func validateCIDRSet(description string, values []string) error {
	seen := map[string]struct{}{}
	for _, value := range values {
		prefix, err := canonicalIPv4Prefix(description, value)
		if err != nil {
			return err
		}
		canonical := prefix.String()
		if _, duplicate := seen[canonical]; duplicate {
			return fmt.Errorf("%s is duplicated: %s", description, canonical)
		}
		seen[canonical] = struct{}{}
	}
	return nil
}

func validSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func contains(values []string, expected string) bool {
	sorted := sortedCopy(values)
	index := sort.SearchStrings(sorted, expected)
	return index < len(sorted) && sorted[index] == expected
}

func sortedCopy(values []string) []string {
	result := append([]string(nil), values...)
	sort.Strings(result)
	return result
}

func (foundation Foundation) ResourceInputs() resources.Inputs {
	return resources.Inputs{
		OwnershipLabel:          foundation.Inputs.OwnershipLabel,
		LabPrefix:               foundation.Inputs.LabPrefix,
		APIPort:                 foundation.Inputs.APIPort,
		ClusterDomain:           foundation.Inputs.ClusterDomain,
		NodeImage:               foundation.Inputs.NodeImage,
		CacheHostPath:           foundation.Inputs.CacheHostPath,
		CacheContainerPath:      foundation.Inputs.CacheContainerPath,
		StorageContainerPath:    foundation.Inputs.StorageContainerPath,
		KonnectivityServerImage: foundation.Inputs.KonnectivityServerImage,
		KonnectivityAgentImage:  foundation.Inputs.KonnectivityAgentImage,
	}
}

func (foundation Foundation) Endpoint(address string) string {
	return netip.MustParseAddr(address).String() + ":" + strconv.Itoa(int(foundation.Inputs.APIPort))
}
