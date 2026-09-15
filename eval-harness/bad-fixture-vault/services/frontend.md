---
title: Frontend
type: service
status: active
summary: The chat/UI service. DEFECT B3 — its http-call edge states no condition at all.
depends_on: ["[[MongoDB]]", "[[Redis]]"]
related_to: ["[[Backend]]", "[[Context Service]]", "[[Scheduler]]", "[[Reporting Job]]", "[[Audit Sink]]"]
cross_service:
- target: "[[Backend]]"
  type: http-call
  endpoint: /api/internal/health
  provenance: frontend/src/health.ts:12
tags: [service, chat, ui]
---

# Frontend

DEFECT B3 (condition-less http-call): the edge above is an unconditional-looking http-call — it
carries no `condition:` key, so a question asking "under what condition" has no answer to read
off. A containment check against the stdout blob cannot see this; a relational check with
`condition_present: true` does.
