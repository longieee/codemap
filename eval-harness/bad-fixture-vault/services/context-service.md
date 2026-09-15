---
title: Context Service
type: service
status: active
summary: Injects conversational context. DEFECT B1 — its edge target is off-vault.
depends_on: ["[[Backend]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Partner Gateway]]"
  type: http-call
  endpoint: /api/agents/chat
  condition: resumable_flow_flag == true
  provenance: context-service/src/dispatch.py:411
tags: [service, context]
---

# Context Service

DEFECT B1 (off-vault edge target): `[[Partner Gateway]]` is not a page in this vault, so the
edge dead-ends — the store reports `external_target: true` and the navigation question "which
in-vault service does this call" has no resolvable answer.
