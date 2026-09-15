---
title: Report Gateway
type: service
status: active
summary: Report dispatcher. Carries one resolved call, one dynamic (unresolved-target) call, and the five physical edge families.
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]", "[[Scheduler]]"]
cross_service:
- target: "[[Frontend]]"
  type: http-call
  endpoint: /api/reports/run
  condition: reports_enabled == true
  confidence: high
  grounded: true
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/src/client.py:88
- target: "[[Unresolved Target]]"
  type: dynamic
  endpoint: (var)
  confidence: low
  grounded: false
  unresolved_expr: f"{base_url}/{route}"
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/src/client.py:140
- target: "[[Scheduler]]"
  type: invokes
  role: caller
  schedule: "*/10 * * * *"
  via: cloud-scheduler
  source: both
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/terraform/main.tf:22
- target: "[[Report Queue]]"
  type: subscribes-to
  via: topic-reports
  source: declared
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/terraform/pubsub.tf:5
- target: "[[Frontend]]"
  type: deploy-env
  value: FRONTEND_URL
  source: live
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/terraform/env.tf:9
- target: "[[Report Runner]]"
  type: runs-as
  value: report-runner@example.invalid
  source: declared
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/terraform/iam.tf:14
- target: "[[Report Dataset]]"
  type: in-dataset
  source: declared
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/terraform/bq.tf:18
- target: "[[Report Bucket]]"
  type: reads-from
  store: gcs
  source: declared
  extracted_from: {repo: "report-gateway", sha: "4f1c9a2e7b3d5081", at: "2026-01-01T00:00:00Z"}
  provenance: report-gateway/terraform/bq.tf:27
tags: [service, reports]
---

# Report Gateway

Dispatches report runs to the [[Frontend]]. One outbound call resolves statically; a second
routes through a runtime-assembled URL and does **not** resolve, so it is served as a `dynamic`
edge pointing at the [[Unresolved Target]] stub rather than being dropped. Scheduled by the
[[Scheduler]], subscribes to the [[Report Queue]] topic, runs as the [[Report Runner]] identity, and its
reporting table lives in the [[Report Dataset]] and reads from the [[Report Bucket]].

The six physical families here are the ones `stitcher/infra.py` actually emits — `invokes`
(:322,:336), `subscribes-to` (:366), `deploy-env` (:315), `runs-as` (:311), `in-dataset` (:357)
and `reads-from` (:359). The fixture previously said `invoke` / `pubsub` / `network`, three names
the emitter never produces, so these questions passed against a vocabulary nothing emitted.
