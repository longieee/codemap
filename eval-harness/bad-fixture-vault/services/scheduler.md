---
title: Scheduler
type: service
status: active
summary: Jobs/RBAC service. DEFECT B4 — the `roles` collection is absent from the edge.
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Frontend]]"
  type: shares-datastore
  endpoint: mongo:accessroles,aclentries,agents,groups,users
  provenance: scheduler/scripts/seed_roles.py:17
tags: [service, rbac, jobs]
---

# Scheduler

DEFECT B4 (the self-confirming fixture property): the collection list above does NOT state
`roles`. A containment test against the stdout blob still passes, because `accessroles`
CONTAINS the substring `roles` — so the eval stays green on a vault that no longer carries the
fact. Token-exact endpoint matching is what fails here.
