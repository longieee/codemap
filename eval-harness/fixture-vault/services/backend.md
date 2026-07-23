---
title: Backend
type: service
status: active
summary: Core API service backing the Frontend.
depends_on: ["[[MongoDB]]"]
related_to: ["[[Frontend]]"]
tags: [service, api]
---

# Backend

Core API service. Reads and writes [[MongoDB]]; called by the [[Frontend]] and the
[[Context Service]].
