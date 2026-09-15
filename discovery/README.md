# discovery/

**Read-only cloud discovery** — the physical layer's evidence collector. These scripts snapshot
what is actually deployed in a cloud environment (services, revisions, env wiring, scheduler
jobs), so `stitcher/reconcile.py` can compare the deployed reality against the static IaC and
report deployed-not-in-IaC, declared-not-live and attribute drift.

One subdirectory per provider; `gcp/` is the one implemented today. Nothing here mutates cloud
state — every call is a describe/list — but the snapshot step is the one part of the pipeline that
needs authenticated cloud credentials, which is why it is a separate script rather than a stitcher
module. See `docs/cloud-discovery.md` for the tier model and the per-environment flow.
