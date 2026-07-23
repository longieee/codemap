---
title: Frontend
type: service
status: active
summary: The chat/UI service; dispatches to downstream services and MCP tools.
depends_on: ["[[MongoDB]]", "[[Redis]]"]
related_to: ["[[Backend]]", "[[Context Service]]"]
cross_service:
- target: "[[Redis]]"
  type: data-store
  endpoint: (tcp; ioredis)
  condition: REDIS_ENABLE_OFFLINE_QUEUE (offline queue → indefinite queue on VPC blip → 504 storm)
  provenance: frontend/src/cache/redisClients.ts:46
tags: [service, chat, ui]
---

# Frontend

The chat front end. Persists conversations to [[MongoDB]] and uses [[Redis]] for caching and
rate-limit state. Receives resumable-flow calls from the [[Context Service]].

## Architecture
Node service; ioredis client; dispatches tool calls to downstream MCP servers such as the
[[Monitoring MCP Server]].
