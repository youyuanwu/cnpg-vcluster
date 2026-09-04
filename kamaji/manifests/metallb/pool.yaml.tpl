apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: kamaji-tenant-vips
  namespace: metallb-system
  labels:
    io.cnpg-vcluster.kamaji-lab: kamaji-cnpg-tenants
spec:
  addresses:
    - ${TENANT_A_VIP}/32
    - ${TENANT_B_VIP}/32
  autoAssign: false
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: kamaji-tenant-vips
  namespace: metallb-system
  labels:
    io.cnpg-vcluster.kamaji-lab: kamaji-cnpg-tenants
spec:
  ipAddressPools:
    - kamaji-tenant-vips
