# WorkflowContext

Work Title: Private Node CNPG Tenants
Work ID: private-node-cnpg-tenants
Base Branch: main
Target Branch: feature/private-node-cnpg-tenants
Execution Mode: current-checkout
Repository Identity: github.com/youyuanwu/cnpg-vcluster@8535ee8a332f4e91cf1fc6b751f7379e25ba7fbb
Execution Binding: none
Workflow Mode: full
Review Strategy: local
Review Policy: final-pr-only
Session Policy: continuous
Final Agent Review: enabled
Final Review Mode: multi-model
Final Review Interactive: smart
Final Review Models: gpt-5.6-sol, claude-opus-5
Final Review Specialists: all
Final Review Interaction Mode: parallel
Final Review Specialist Models: none
Final Review Perspectives: auto
Final Review Perspective Cap: 2
Implementation Model: gpt-5.6-sol
Plan Generation Mode: single-model
Plan Generation Models: gpt-5.6-sol
Planning Docs Review: enabled
Planning Review Mode: multi-model
Planning Review Interactive: smart
Planning Review Models: gpt-5.6-sol, claude-opus-5
Planning Review Specialists: all
Planning Review Interaction Mode: parallel
Planning Review Specialist Models: none
Planning Review Perspectives: auto
Planning Review Perspective Cap: 2
Custom Workflow Instructions: Use GPT models for specification, research, planning, and implementation. Claude Opus 5 may only be used for review activities. Multi-model planning-document and final reviews must include both gpt-5.6-sol and claude-opus-5.
Initial Prompt: Implement a local single-Docker-host environment with one kind control cluster and two linked private-node vCluster tenant clusters. Each tenant owns exclusive worker-node containers and runs its own CloudNativePG operator plus one three-instance PostgreSQL Cluster. Update and carry docs/high-level-design.md with the implementation.
Issue URL: none
Remote: origin
Artifact Lifecycle: commit-and-clean
Artifact Paths: auto-derived
Additional Inputs: Existing docs/high-level-design.md; Docker 29.7.2 with 16 CPUs and approximately 31 GiB RAM is available on the development machine.
