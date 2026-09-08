apiVersion: bootstrap.cluster.x-k8s.io/v1beta2
kind: KubeadmConfigTemplate
metadata:
  name: ${CLUSTER_NAME}-worker
  namespace: ${NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  template:
    spec:
      joinConfiguration:
        nodeRegistration:
          kubeletExtraArgs:
            - name: eviction-hard
              value: nodefs.available<0%,nodefs.inodesFree<0%,imagefs.available<0%
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: DevMachineTemplate
metadata:
  name: ${CLUSTER_NAME}-worker
  namespace: ${NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  template:
    spec:
      backend:
        docker:
          customImage: ${KIND_NODE_IMAGE}
          bootstrapTimeout: 5m
          preLoadImages:
${WORKER_PRELOAD_IMAGES}
          extraMounts:
            - hostPath: ${STORAGE_HOST_PATH}
              containerPath: ${STORAGE_CONTAINER_PATH}
              readOnly: false
---
apiVersion: cluster.x-k8s.io/v1beta2
kind: MachineDeployment
metadata:
  name: ${CLUSTER_NAME}-worker
  namespace: ${NAMESPACE}
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  clusterName: ${CLUSTER_NAME}
  replicas: ${WORKER_REPLICAS}
  machineNaming:
    template: "{{ .cluster.name }}-worker-{{ .random }}"
  selector:
    matchLabels:
      cluster.x-k8s.io/cluster-name: ${CLUSTER_NAME}
      cnpg-vcluster.capi/nodepool: worker
  template:
    metadata:
      labels:
        cluster.x-k8s.io/cluster-name: ${CLUSTER_NAME}
        cnpg-vcluster.capi/nodepool: worker
        ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
    spec:
      clusterName: ${CLUSTER_NAME}
      version: ${KUBERNETES_VERSION}
      bootstrap:
        configRef:
          apiGroup: bootstrap.cluster.x-k8s.io
          kind: KubeadmConfigTemplate
          name: ${CLUSTER_NAME}-worker
      infrastructureRef:
        apiGroup: infrastructure.cluster.x-k8s.io
        kind: DevMachineTemplate
        name: ${CLUSTER_NAME}-worker
