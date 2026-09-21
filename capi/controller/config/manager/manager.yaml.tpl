apiVersion: apps/v1
kind: Deployment
metadata:
  name: tenant-controller
  namespace: tenant-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: tenant-controller
  template:
    metadata:
      labels:
        app.kubernetes.io/name: tenant-controller
    spec:
      serviceAccountName: tenant-controller
      containers:
      - name: manager
        image: ${TENANT_CONTROLLER_IMAGE}
        imagePullPolicy: Never
        args:
        - --leader-elect=true
        - --mutation-enabled=false
        - --controller-image=${TENANT_CONTROLLER_IMAGE}
        - --supported-kubernetes-version=${SUPPORTED_KUBERNETES_VERSION}
        - --webhook-cert-dir=/var/run/tenant-controller/tls
        ports:
        - name: webhook
          containerPort: 9443
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
        - name: webhook-cert
          mountPath: /var/run/tenant-controller/tls
          readOnly: true
        - name: docker-socket
          mountPath: /var/run/docker.sock
      volumes:
      - name: webhook-cert
        secret:
          secretName: tenant-controller-serving-cert
      - name: docker-socket
        hostPath:
          path: /var/run/docker.sock
          type: Socket
