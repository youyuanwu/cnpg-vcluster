{
  "apiVersion": "apps/v1",
  "kind": "Deployment",
  "metadata": {
    "name": "tenant-admin",
    "namespace": "tenant-system"
  },
  "spec": {
    "progressDeadlineSeconds": 300,
    "replicas": 1,
    "revisionHistoryLimit": 2,
    "selector": {
      "matchLabels": {
        "app.kubernetes.io/name": "tenant-admin"
      }
    },
    "strategy": {
      "type": "Recreate"
    },
    "template": {
      "metadata": {
        "labels": {
          "app.kubernetes.io/name": "tenant-admin"
        }
      },
      "spec": {
        "automountServiceAccountToken": true,
        "containers": [
          {
            "env": [
              {
                "name": "TENANT_ADMIN_PROVIDER",
                "value": "azure"
              }
            ],
            "image": "${TENANT_ADMIN_IMAGE}",
            "imagePullPolicy": "IfNotPresent",
            "livenessProbe": {
              "failureThreshold": 3,
              "httpGet": {
                "path": "/healthz",
                "port": "http"
              },
              "initialDelaySeconds": 5,
              "periodSeconds": 10,
              "timeoutSeconds": 2
            },
            "name": "admin",
            "ports": [
              {
                "containerPort": 8080,
                "name": "http",
                "protocol": "TCP"
              }
            ],
            "readinessProbe": {
              "failureThreshold": 3,
              "httpGet": {
                "path": "/readyz",
                "port": "http"
              },
              "periodSeconds": 5,
              "timeoutSeconds": 2
            },
            "resources": {
              "limits": {
                "cpu": "250m",
                "memory": "128Mi"
              },
              "requests": {
                "cpu": "25m",
                "memory": "32Mi"
              }
            },
            "securityContext": {
              "allowPrivilegeEscalation": false,
              "capabilities": {
                "drop": [
                  "ALL"
                ]
              },
              "privileged": false,
              "readOnlyRootFilesystem": true,
              "runAsNonRoot": true
            }
          }
        ],
        "enableServiceLinks": false,
        "securityContext": {
          "runAsGroup": 65532,
          "runAsNonRoot": true,
          "runAsUser": 65532,
          "seccompProfile": {
            "type": "RuntimeDefault"
          }
        },
        "serviceAccountName": "tenant-admin",
        "terminationGracePeriodSeconds": 30
      }
    }
  }
}
