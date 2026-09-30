apiVersion: apps/v1
kind: Deployment
metadata:
  name: database-admission
  namespace: tenant-system
spec:
  replicas: 2
  selector:
    matchLabels:
      app.kubernetes.io/name: database-admission
  template:
    metadata:
      labels:
        app.kubernetes.io/name: database-admission
      annotations:
        tenancy.cnpg-vcluster.io/source-digest: "${SOURCE_DIGEST}"
    spec:
      serviceAccountName: database-admission
      automountServiceAccountToken: true
      enableServiceLinks: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: admission
          image: "${ADMISSION_IMAGE}"
          imagePullPolicy: IfNotPresent
          ports:
            - name: https
              containerPort: 9443
          env:
            - name: ADMISSION_CERT_FILE
              value: /cert/tls.crt
            - name: ADMISSION_KEY_FILE
              value: /cert/tls.key
          volumeMounts:
            - name: serving-certificate
              mountPath: /cert
              readOnly: true
          readinessProbe:
            tcpSocket:
              port: https
            periodSeconds: 5
          livenessProbe:
            tcpSocket:
              port: https
            periodSeconds: 10
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 500m
              memory: 256Mi
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop:
                - ALL
      volumes:
        - name: serving-certificate
          secret:
            secretName: database-admission-serving
