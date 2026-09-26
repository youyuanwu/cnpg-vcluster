apiVersion: apps/v1
kind: Deployment
metadata:
  name: tenant-controller
  namespace: tenant-system
  annotations:
    tenancy.cnpg-vcluster.io/lifecycle-epoch: ${CONTROLLER_LIFECYCLE_EPOCH}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: tenant-controller
  template:
    metadata:
      labels:
        app.kubernetes.io/name: tenant-controller
      annotations:
        tenancy.cnpg-vcluster.io/lifecycle-epoch: ${CONTROLLER_LIFECYCLE_EPOCH}
    spec:
      serviceAccountName: tenant-controller
      terminationGracePeriodSeconds: 60
      containers:
      - name: manager
        image: ${TENANT_CONTROLLER_IMAGE}
        imagePullPolicy: Never
        args:
        - --leader-elect=true
        - --mutation-enabled=${CONTROLLER_MUTATION_ENABLED}
        - --lifecycle-epoch=${CONTROLLER_LIFECYCLE_EPOCH}
        - --controller-image=${TENANT_CONTROLLER_IMAGE}
        - --supported-kubernetes-version=${SUPPORTED_KUBERNETES_VERSION}
        - --health-probe-bind-address=0.0.0.0:8081
        env:
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        ports:
        - name: health
          containerPort: 8081
        livenessProbe:
          httpGet:
            path: /healthz
            port: health
        readinessProbe:
          httpGet:
            path: /readyz
            port: health
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
        volumeMounts:
        - name: docker-socket
          mountPath: /var/run/docker.sock
      volumes:
      - name: docker-socket
        hostPath:
          path: /var/run/docker.sock
          type: Socket
