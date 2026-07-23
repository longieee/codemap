---
title: Scheduler
type: service
status: active
summary: Jobs/RBAC service; shares the platform datastore with the Frontend (no direct call).
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Frontend]]"
  type: shares-datastore
  endpoint: mongo:accessroles,aclentries,agents,groups,roles,users
  provenance: scheduler/scripts/seed_roles.py:17
tags: [service, rbac, jobs]
---

# Scheduler

Jobs + RBAC backend. Writes RBAC collections (`roles`, `users`, `agents`, …) in [[MongoDB]] that
the [[Frontend]] reads — the two are coupled through the shared store with **no HTTP call between
them**.
