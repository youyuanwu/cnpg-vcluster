apiVersion: v1
kind: ServiceAccount
metadata:
  name: capi-kube-proxy
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: capi-system:node-proxier
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:node-proxier
subjects:
  - kind: ServiceAccount
    name: capi-kube-proxy
    namespace: kube-system
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: capi-kube-proxy
  namespace: kube-system
data:
  config.conf: |-
    apiVersion: kubeproxy.config.k8s.io/v1alpha1
    kind: KubeProxyConfiguration
    bindAddress: 0.0.0.0
    clientConnection:
      kubeconfig: /var/lib/kube-proxy/kubeconfig.conf
    clusterCIDR: ${POD_CIDR}
    conntrack:
      maxPerCore: 0
      min: 0
    mode: iptables
  kubeconfig.conf: |-
    apiVersion: v1
    kind: Config
    clusters:
      - cluster:
          certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
          server: https://${API_VIP}:${API_PORT}
        name: default
    contexts:
      - context:
          cluster: default
          namespace: default
          user: default
        name: default
    current-context: default
    users:
      - name: default
        user:
          tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: capi-kube-proxy
  namespace: kube-system
  labels:
    k8s-app: capi-kube-proxy
spec:
  selector:
    matchLabels:
      k8s-app: capi-kube-proxy
  updateStrategy:
    type: RollingUpdate
  template:
    metadata:
      labels:
        k8s-app: capi-kube-proxy
    spec:
      priorityClassName: system-node-critical
      serviceAccountName: capi-kube-proxy
      hostNetwork: true
      tolerations:
        - operator: Exists
      containers:
        - name: kube-proxy
          image: ${KUBE_PROXY_IMAGE}
          command:
            - /usr/local/bin/kube-proxy
            - --config=/var/lib/kube-proxy/config.conf
            - --v=2
          securityContext:
            privileged: true
          volumeMounts:
            - name: kube-proxy
              mountPath: /var/lib/kube-proxy
            - name: xtables-lock
              mountPath: /run/xtables.lock
            - name: lib-modules
              mountPath: /lib/modules
              readOnly: true
      volumes:
        - name: kube-proxy
          configMap:
            name: capi-kube-proxy
        - name: xtables-lock
          hostPath:
            path: /run/xtables.lock
            type: FileOrCreate
        - name: lib-modules
          hostPath:
            path: /lib/modules
            type: Directory
