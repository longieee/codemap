---
title: Context Service
type: service
status: active
summary: Injects conversational context; calls the Frontend's resumable-flow endpoint under a flag.
depends_on: ["[[Backend]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Frontend]]"
  type: http-call
  endpoint: /api/agents/chat
  condition: resumable_flow_flag == true
  provenance: context-service/src/helper_service.py:411
tags: [service, context]
---

# Context Service

Context-injection service. When `resumable_flow_flag` is enabled, it dispatches to the
[[Frontend]]'s `/api/agents/chat` endpoint (see the dispatcher at `helper_service.py:411`).
Depends on the [[Backend]] for lookups.
