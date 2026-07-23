---
title: Cleanup Job
type: service
status: active
summary: Cloud Run job that prunes old conversations; coupled to the Frontend via the shared store.
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Frontend]]"
  type: shares-datastore
  endpoint: mongo:conversations,messages,sharedlinks,toolcalls
  provenance: cleanup-job/main.py:107
tags: [service, job, maintenance]
---

# Cleanup Job

Scheduled job that prunes stale conversations from [[MongoDB]]. Reads/deletes the same
conversation collections the [[Frontend]] owns — a shares-datastore coupling with no direct call.
