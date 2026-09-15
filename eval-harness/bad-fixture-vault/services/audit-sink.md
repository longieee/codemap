---
title: Audit Sink
type: service
status: active
summary: Append-only audit consumer. DEFECT B5 — its provenance pointer is stale.
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]"]
cross_service:
- target: "[[Frontend]]"
  type: data-store
  endpoint: (tcp)
  condition: AUDIT_TCP_ENABLED
  provenance: audit-sink/src/sink.py:9
tags: [service, audit]
---

# Audit Sink

DEFECT B5 (stale provenance pointer): the edge still points at `sink.py:9`, the line the call
used to live on. A map whose provenance no longer resolves is a map you cannot verify against
code — which is the honest bound the eval exists to hold.
