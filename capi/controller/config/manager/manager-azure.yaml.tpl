apiVersion: apps/v1
kind: Deployment
metadata:
  name: tenant-controller
  namespace: tenant-system
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
    spec:
      serviceAccountName: tenant-controller
      terminationGracePeriodSeconds: 60
      containers:
      - name: manager
        image: ${TENANT_CONTROLLER_IMAGE}
        imagePullPolicy: IfNotPresent
        args:
        - --provider=azure
        - --leader-elect=true
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
