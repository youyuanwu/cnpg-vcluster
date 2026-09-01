SHELL := /usr/bin/env bash

.PHONY: tools create verify status diagnose destroy test-static test-e2e

tools:
	@./scripts/tools.sh

create: tools
	@./scripts/create.sh

verify: tools
	@./scripts/verify.sh

status: tools
	@./scripts/status.sh

diagnose: tools
	@./scripts/diagnose.sh $(TENANT)

destroy: tools
	@./scripts/destroy.sh

test-static:
	@./scripts/test-static.sh

test-e2e: tools
	@./scripts/test-e2e.sh
