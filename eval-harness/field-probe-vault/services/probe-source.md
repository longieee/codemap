---
title: Probe Source
type: service
status: active
summary: Carries one edge of every family with every field the emitter can write.
related_to: ["[[Probe Target]]"]
cross_service:
- target: "[[Probe Target]]"
  type: dynamic
  endpoint: (var)
  confidence: low
  grounded: false
  unresolved_expr: f"{base_url}/v1/{name}"
  endpoint_expr: f"{base_url}/v1/{name}"
  extracted_from: {repo: "probe-repo", sha: "0123456789abcdef", at: "2026-01-01T00:00:00Z"}
  provenance: probe-repo/src/client.py:42
- target: "[[Probe Target]]"
  type: http-call
  endpoint: /api/probe
  confidence: high
  grounded: true
  condition: probe_flag == true
  extracted_from: {repo: "probe-repo", sha: "0123456789abcdef", at: "2026-01-01T00:00:00Z"}
  provenance: probe-repo/src/client.py:99
- target: "[[Probe Store]]"
  type: shares-datastore
  store: mongo
  endpoint: mongo:probe_items,probe_runs
  collections: [probe_items, probe_runs]
  access: {this: [r, w], target: [r]}
  owner: "[[Probe Target]]"
  bidirectional: true
  provenance: probe-repo/scripts/seed.py:7
- target: "[[Probe Target]]"
  type: invokes
  role: caller
  schedule: "*/5 * * * *"
  via: cloud-scheduler
  source: both
  provenance: probe-repo/terraform/main.tf:10
- target: "[[Probe Queue]]"
  type: subscribes-to
  via: topic-probe
  source: declared
  provenance: probe-repo/terraform/pubsub.tf:3
- target: "[[Probe Env]]"
  type: deploy-env
  value: PROBE_URL
  source: live
  provenance: probe-repo/terraform/env.tf:7
- target: "[[Probe Identity]]"
  type: runs-as
  value: probe-runner@example.invalid
  source: declared
  provenance: probe-repo/terraform/iam.tf:12
- target: "[[Probe Net]]"
  type: reads-from
  store: gcs
  via: connector-probe
  source: live
  provenance: probe-repo/terraform/net.tf:20
tags: [service, probe]
---

# Probe Source

Not a navigation fixture. This page exists so the eval can ask the SERVING BINARY which edge
fields it actually passes through, instead of assuming. Every field the emitter
(`stitcher/emit.py`) can write appears above exactly once, on a real edge of its own family, so a
field missing from `fmg xedges --format json` against this vault is a field the store drops --
which is a different fact from "this vault's edges do not carry it", and must not be reported as
the same thing.
