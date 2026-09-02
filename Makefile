VCLUSTER_DIR := vcluster
VCLUSTER_TARGETS := tools create verify status diagnose destroy test-static test-e2e

.PHONY: $(VCLUSTER_TARGETS)

$(VCLUSTER_TARGETS):
	@$(MAKE) -C $(VCLUSTER_DIR) $@
