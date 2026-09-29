apiVersion: apps/v1
kind: Deployment
metadata:
  name: tenant-admin
  namespace: tenant-system
spec:
  replicas: 1
  revisionHistoryLimit: 2
  progressDeadlineSeconds: 300
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: tenant-admin
  template:
    metadata:
      labels:
        app.kubernetes.io/name: tenant-admin
    spec:
      serviceAccountName: tenant-admin
      automountServiceAccountToken: true
      enableServiceLinks: false
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
      - name: admin
        image: ${TENANT_ADMIN_IMAGE}
        imagePullPolicy: IfNotPresent
        env:
        - name: TENANT_ADMIN_PROVIDER
          value: azure
        ports:
        - name: http
          containerPort: 8080
          protocol: TCP
        livenessProbe:
          httpGet:
            path: /healthz
            port: http
          initialDelaySeconds: 5
          periodSeconds: 10
          timeoutSeconds: 2
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /readyz
            port: http
          periodSeconds: 5
          timeoutSeconds: 2
          failureThreshold: 3
        securityContext:
          runAsNonRoot: true
          privileged: false
          allowPrivilegeEscalation: false
          readOnlyRootFilesystem: true
          capabilities:
            drop:
            - ALL
        resources:
          requests:
            cpu: 25m
            memory: 32Mi
          limits:
            cpu: 250m
            memory: 128Mi
