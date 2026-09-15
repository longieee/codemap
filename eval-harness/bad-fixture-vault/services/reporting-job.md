---
title: Reporting Job
type: service
status: active
summary: Export job. DEFECT B2 — its endpoint is an unresolved {template}.
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Frontend]]"
  type: http-call
  endpoint: /api/{tenant}/export
  condition: export_enabled == true
  provenance: reporting-job/main.py:88
tags: [service, job, analytics]
---

# Reporting Job

DEFECT B2 (unresolved template endpoint): `{tenant}` was never resolved during extraction, so
the served endpoint is a shape, not an address. `endpoint_resolved: true` fails on it.
