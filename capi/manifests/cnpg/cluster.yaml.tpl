apiVersion: v1
kind: Namespace
metadata:
  name: database
---
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: ${CNPG_CLUSTER}
  namespace: database
spec:
  instances: ${CNPG_INSTANCES}
  imageName: ${POSTGRES_IMAGE}
  affinity:
    enablePodAntiAffinity: true
    podAntiAffinityType: ${CNPG_ANTI_AFFINITY_TYPE}
    topologyKey: kubernetes.io/hostname
  bootstrap:
    initdb:
      database: app
      owner: app
  storage:
    size: 1Gi
    storageClass: ${STORAGE_CLASS}
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: "1"
      memory: 1Gi
