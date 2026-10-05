# Tenant Admin resource explorer performance check

The resource explorer uses current snapshot data and does not fetch or stream
additional graph data during interaction. Its native model test constructs
2,000 nodes and 5,000 relationships to guard filtering, selection, focused
layout input, inspection, and one-group expansion against algorithmic
regressions.

Use the following browser protocol after `just admin-build` when changing the
explorer presentation:

1. Serve the built Tenant Admin application with a fixture containing 2,000
   resources and 5,000 relationships. Include every architecture band, at
   least two database clusters with primary/standby instances, authoritative
   full and partial placement, provider-owned resources and unknown values.
2. Open browser developer tools, enable performance recording, and load the
   Tenant **Resources** section.
3. Record each operation separately: search, health filter, resource
   selection, relationship focus, and one-group expansion.
4. Measure from the input or activation event until the inventory, graph, and
   inspector have completed their visible update.
5. Confirm every operation completes within one second and the focused graph
   contains no more than 20 resource nodes.
6. Repeat at 320, 768, and 1280 CSS-pixel viewport widths and confirm overflow
   remains inside the graph workspace rather than the page.

The browser measurement is the acceptance evidence for visible latency. The
native timing assertion is supporting regression evidence and is not a
substitute for browser rendering measurement.
