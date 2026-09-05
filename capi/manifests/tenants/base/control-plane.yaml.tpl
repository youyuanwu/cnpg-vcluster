apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
---
apiVersion: cluster.x-k8s.io/v1beta2
kind: Cluster
metadata:
  name: ${CLUSTER_NAME}
  namespace: ${NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  controlPlaneEndpoint:
    host: ${API_VIP}
    port: ${API_PORT}
  clusterNetwork:
    apiServerPort: ${API_PORT}
    services:
      cidrBlocks:
        - ${SERVICE_CIDR}
    pods:
      cidrBlocks:
        - ${POD_CIDR}
    serviceDomain: ${CLUSTER_DOMAIN}
  infrastructureRef:
    apiGroup: infrastructure.cluster.x-k8s.io
    kind: DevCluster
    name: ${CLUSTER_NAME}
  controlPlaneRef:
    apiGroup: controlplane.cluster.x-k8s.io
    kind: KamajiControlPlane
    name: ${CLUSTER_NAME}
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: DevCluster
metadata:
  name: ${CLUSTER_NAME}
  namespace: ${NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  controlPlaneEndpoint:
    host: ${API_VIP}
    port: ${API_PORT}
  backend:
    docker:
      loadBalancer: {}
---
apiVersion: controlplane.cluster.x-k8s.io/v1alpha2
kind: KamajiControlPlane
metadata:
  name: ${CLUSTER_NAME}
  namespace: ${NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  version: ${KUBERNETES_VERSION}
  replicas: 1
  dataStoreName: default
  network:
    serviceType: LoadBalancer
    serviceAddress: ${API_VIP}
    serviceAnnotations:
      metallb.io/loadBalancerIPs: ${API_VIP}
    certSANs:
      - ${API_VIP}
      - ${CLUSTER_NAME}
    dnsServiceIPs:
      - ${DNS_SERVICE_IP}
  addons:
    coreDNS:
      dnsServiceIPs:
        - ${DNS_SERVICE_IP}
    konnectivity:
      server:
        image: ${KONNECTIVITY_SERVER_REPOSITORY}
        version: ${KONNECTIVITY_SERVER_VERSION_DIGEST}
        port: 8132
      agent:
        image: ${KONNECTIVITY_AGENT_REPOSITORY}
        version: ${KONNECTIVITY_AGENT_VERSION_DIGEST}
        mode: DaemonSet
        hostNetwork: true
        tolerations:
          - key: CriticalAddonsOnly
            operator: Exists
          - key: node.kubernetes.io/not-ready
            operator: Exists
            effect: NoSchedule
          - key: node.kubernetes.io/not-ready
            operator: Exists
            effect: NoExecute
