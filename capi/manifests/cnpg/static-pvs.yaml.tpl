apiVersion: v1
kind: List
items:
  - apiVersion: v1
    kind: PersistentVolume
    metadata:
      name: ${CNPG_CLUSTER}-pv-1
    spec:
      capacity:
        storage: 1Gi
      accessModes: [ReadWriteOnce]
      persistentVolumeReclaimPolicy: Retain
      storageClassName: ${STORAGE_CLASS}
      claimRef:
        namespace: database
        name: ${CNPG_CLUSTER}-1
      hostPath:
        path: ${STORAGE_PATH}/volumes/cnpg/1
        type: DirectoryOrCreate
  - apiVersion: v1
    kind: PersistentVolume
    metadata:
      name: ${CNPG_CLUSTER}-pv-2
    spec:
      capacity:
        storage: 1Gi
      accessModes: [ReadWriteOnce]
      persistentVolumeReclaimPolicy: Retain
      storageClassName: ${STORAGE_CLASS}
      claimRef:
        namespace: database
        name: ${CNPG_CLUSTER}-2
      hostPath:
        path: ${STORAGE_PATH}/volumes/cnpg/2
        type: DirectoryOrCreate
  - apiVersion: v1
    kind: PersistentVolume
    metadata:
      name: ${CNPG_CLUSTER}-pv-3
    spec:
      capacity:
        storage: 1Gi
      accessModes: [ReadWriteOnce]
      persistentVolumeReclaimPolicy: Retain
      storageClassName: ${STORAGE_CLASS}
      claimRef:
        namespace: database
        name: ${CNPG_CLUSTER}-3
      hostPath:
        path: ${STORAGE_PATH}/volumes/cnpg/3
        type: DirectoryOrCreate
