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
  store: mongo
  endpoint: mongo:conversations,messages,sharedlinks,toolcalls
  collections: [conversations, messages, sharedlinks, toolcalls]
  access: {this: [r, w], target: [r, w]}
  owner: "[[Frontend]]"
  bidirectional: true
  extracted_from: {repo: "cleanup-job", sha: "8b2e40d1c97a6f35", at: "2026-01-01T00:00:00Z"}
  provenance: cleanup-job/main.py:107
tags: [service, job, maintenance]
---

# Cleanup Job

Scheduled job that prunes stale conversations from [[MongoDB]]. Reads/deletes the same
conversation collections the [[Frontend]] owns — a shares-datastore coupling with no direct call.
