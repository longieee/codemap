---
title: Usage Monitor
type: service
status: active
summary: Cloud Run job that ingests the platform DB into a warehouse; coupled via the shared store.
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Frontend]]"
  type: shares-datastore
  endpoint: mongo:agents,conversations,conversationtags,messages,transactions,users
  provenance: usage-monitor/terraform/main.tf:135 (bq external table)
tags: [service, job, analytics]
---

# Usage Monitor

Scheduled Cloud Run job. Ingests conversation/message collections owned by the [[Frontend]] into a
warehouse via a BigQuery external table — coupled to the Frontend through the shared [[MongoDB]]
store, not through any call.
