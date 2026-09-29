#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

ADMIN_NAME = "tenant-admin"
ADMIN_ROLE_NAME = "tenant-admin"
ADMIN_NAMESPACE = "tenant-system"
ADMIN_IMAGE = "${TENANT_ADMIN_IMAGE}"
ADMIN_CONTAINER_PORT = 8080
ADMIN_SERVICE_PORT = 80

READ_ONLY_RESOURCES = (
    (
        "",
        (
            "configmaps",
            "namespaces",
            "persistentvolumeclaims",
            "services",
        ),
    ),
    ("apps", ("deployments", "statefulsets")),
    ("batch", ("jobs",)),
    (
        "bootstrap.cluster.x-k8s.io",
        ("kubeadmconfigs", "kubeadmconfigtemplates"),
    ),
    (
        "cert-manager.io",
        ("certificates", "certificaterequests", "issuers"),
    ),
    (
        "cluster.x-k8s.io",
        (
            "clusters",
            "machinedeployments",
            "machinepools",
            "machines",
            "machinesets",
        ),
    ),
    ("controlplane.cluster.x-k8s.io", ("kamajicontrolplanes",)),
    ("coordination.k8s.io", ("leases",)),
    (
        "infrastructure.cluster.x-k8s.io",
        (
            "azureclusteridentities",
            "azureclusters",
            "azuremachinepoolmachines",
            "azuremachinepools",
            "devclusters",
            "devmachines",
            "devmachinetemplates",
        ),
    ),
    ("kamaji.clastix.io", ("tenantcontrolplanes",)),
    (
        "network.azure.com",
        ("natgateways", "virtualnetworkssubnets", "virtualnetworks"),
    ),
    ("policy", ("poddisruptionbudgets",)),
    ("rbac.authorization.k8s.io", ("rolebindings", "roles")),
    ("resources.azure.com", ("resourcegroups",)),
    ("tenancy.cnpg-vcluster.io", ("tenants",)),
)

OUTPUT_PATHS = (
    Path("admin/config/deployment/deployment-azure.yaml.tpl"),
    Path("admin/config/deployment/deployment-local.yaml.tpl"),
    Path("admin/config/rbac/cluster-role-binding.yaml"),
    Path("admin/config/rbac/cluster-role.yaml"),
    Path("admin/config/rbac/service-account.yaml"),
    Path("admin/config/service/service.yaml"),
)


def _cluster_role() -> str:
    lines = [
        "---",
        "apiVersion: rbac.authorization.k8s.io/v1",
        "kind: ClusterRole",
        "metadata:",
        f"  name: {ADMIN_ROLE_NAME}",
        "rules:",
    ]
    for api_group, resources in READ_ONLY_RESOURCES:
        rendered_group = f"'{api_group}'" if not api_group else api_group
        lines.extend(
            (
                "- apiGroups:",
                f"  - {rendered_group}",
                "  resources:",
                *(f"  - {resource}" for resource in resources),
                "  verbs:",
                "  - get",
                "  - list",
            )
        )
    return "\n".join(lines) + "\n"


def _deployment(provider: str) -> str:
    if provider not in {"local", "azure"}:
        raise ValueError(f"unsupported admin provider: {provider}")
    image_pull_policy = "Never" if provider == "local" else "IfNotPresent"
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {ADMIN_NAME}
  namespace: {ADMIN_NAMESPACE}
spec:
  replicas: 1
  revisionHistoryLimit: 2
  progressDeadlineSeconds: 300
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: {ADMIN_NAME}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {ADMIN_NAME}
    spec:
      serviceAccountName: {ADMIN_NAME}
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
        image: {ADMIN_IMAGE}
        imagePullPolicy: {image_pull_policy}
        env:
        - name: TENANT_ADMIN_PROVIDER
          value: {provider}
        ports:
        - name: http
          containerPort: {ADMIN_CONTAINER_PORT}
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
"""


def generated_documents() -> dict[Path, str]:
    return {
        Path("admin/config/deployment/deployment-azure.yaml.tpl"): _deployment(
            "azure"
        ),
        Path("admin/config/deployment/deployment-local.yaml.tpl"): _deployment(
            "local"
        ),
        Path("admin/config/rbac/cluster-role-binding.yaml"): f"""\
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {ADMIN_NAME}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {ADMIN_ROLE_NAME}
subjects:
- kind: ServiceAccount
  name: {ADMIN_NAME}
  namespace: {ADMIN_NAMESPACE}
""",
        Path("admin/config/rbac/cluster-role.yaml"): _cluster_role(),
        Path("admin/config/rbac/service-account.yaml"): f"""\
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {ADMIN_NAME}
  namespace: {ADMIN_NAMESPACE}
automountServiceAccountToken: false
""",
        Path("admin/config/service/service.yaml"): f"""\
apiVersion: v1
kind: Service
metadata:
  name: {ADMIN_NAME}
  namespace: {ADMIN_NAMESPACE}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: {ADMIN_NAME}
  ports:
  - name: http
    port: {ADMIN_SERVICE_PORT}
    targetPort: {ADMIN_CONTAINER_PORT}
    protocol: TCP
""",
    }


def generate(root: Path, *, check: bool) -> bool:
    documents = generated_documents()
    if tuple(sorted(documents)) != OUTPUT_PATHS:
        raise RuntimeError("admin resource output set does not match OUTPUT_PATHS")

    valid = True
    for relative_path in OUTPUT_PATHS:
        path = root / relative_path
        expected = documents[relative_path]
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                print(f"generated admin resource is stale: {path}", file=sys.stderr)
                valid = False
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
    return valid


def main(arguments: list[str]) -> int:
    if arguments not in ([], ["--check"]):
        print("usage: generate_admin_resources.py [--check]", file=sys.stderr)
        return 2
    return 0 if generate(ROOT, check=arguments == ["--check"]) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
