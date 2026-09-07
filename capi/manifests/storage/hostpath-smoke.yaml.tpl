apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ${STORAGE_CLASS}
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: Immediate
reclaimPolicy: Retain
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${PV_NAME}
spec:
  capacity:
    storage: 32Mi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ${STORAGE_CLASS}
  claimRef:
    namespace: default
    name: storage-smoke
  hostPath:
    path: ${STORAGE_PATH}/volumes/smoke
    type: DirectoryOrCreate
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: storage-smoke
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ${STORAGE_CLASS}
  volumeName: ${PV_NAME}
  resources:
    requests:
      storage: 32Mi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: storage-smoke
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: storage-smoke
  template:
    metadata:
      labels:
        app: storage-smoke
    spec:
      containers:
        - name: smoke
          image: ${VERIFY_IMAGE}
          command:
            - sh
            - -ec
            - test -f /data/marker || echo machine-independent > /data/marker; sleep 3600
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: storage-smoke
