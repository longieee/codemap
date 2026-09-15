---
title: Bad Report Gateway
type: service
status: active
summary: Carries one seeded defect per NEW edge family — dynamic mislabelling, physical provenance, freshness, and datastore access.
related_to: ["[[Frontend]]", "[[Bad Report Queue]]"]
cross_service:
- target: "[[Bad Unresolved Target]]"
  type: dynamic
  endpoint: (var)
  unresolved_expr: f"{gateway_base}/{route}"
  provenance: bad-report-gateway/src/client.py:140
  extracted_from: {repo: "bad-report-gateway", sha: "unknown", at: "2026-01-01T00:00:00Z"}
- target: "[[Frontend]]"
  type: dynamic
  endpoint: /api/reports/run
  provenance: bad-report-gateway/src/client.py:88
  extracted_from: {repo: "bad-report-gateway", sha: "1a2b3c4d5e6f7081", at: "2026-01-01T00:00:00Z"}
- target: "[[Bad Report Queue]]"
  type: subscribes-to
  extracted_from: {repo: "bad-report-gateway", sha: "1a2b3c4d5e6f7081", at: "2026-01-01T00:00:00Z"}
- target: "[[Frontend]]"
  type: invokes
  role: caller
  provenance: bad-report-gateway/terraform/main.tf:22
  extracted_from: {repo: "bad-report-gateway", sha: "1a2b3c4d5e6f7081"}
- target: "[[Frontend]]"
  type: shares-datastore
  store: mongo
  endpoint: mongo:reports,report_runs
  collections: [reports]
  access: {this: [r], target: [r, w]}
  owner: "[[Frontend]]"
  provenance: bad-report-gateway/main.py:61
  extracted_from: {repo: "bad-report-gateway", sha: "1a2b3c4d5e6f7081", at: "2026-01-01T00:00:00Z"}
tags: [service, reports]
---

# Bad Report Gateway

One seeded defect per new family, each detectable only by a relational read of the served record:

- **B8 dynamic mislabelling.** Two `dynamic` edges. The first is honest (`endpoint: (var)`). The
  second carries a fully resolved endpoint while still typed `dynamic` — so a check that merely
  asserts "a dynamic edge is present" passes, and the family stops being *distinguishable* from
  a resolved one. Only a negative assertion (`expect_absent`) can say so.
- **B9 physical edge with no provenance.** The `subscribes-to` edge names a target and a type and
  nothing that can be checked against a declaration file.
- **B10 stale freshness pointer.** The first edge's `extracted_from.sha` is the literal
  `unknown`, so the record cannot be tied to a commit.
- **B11 incomplete freshness record.** The `invokes` edge's `extracted_from` omits `at`, so
  nothing says when the extraction happened.
- **B12 collections list narrower than the endpoint string.** `endpoint` still advertises
  `mongo:reports,report_runs` while `collections` lists only `reports` — the flat string looks
  complete and the structured field the consumer reads is not.
- **B13 access understated.** The edge writes both collections in reality but `access.this`
  claims read-only, so a blast-radius question gets the wrong answer.
