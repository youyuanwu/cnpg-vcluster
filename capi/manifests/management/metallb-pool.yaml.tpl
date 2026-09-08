apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: capi-tenant-vips
  namespace: metallb-system
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  addresses:
    - ${VIP_POOL_START}-${VIP_POOL_END}
  autoAssign: false
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: capi-tenant-vips
  namespace: metallb-system
  labels:
    ${OWNERSHIP_LABEL}: ${LAB_PREFIX}
spec:
  ipAddressPools:
    - capi-tenant-vips
