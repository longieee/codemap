---
title: Edge Relay
type: service
status: active
summary: Head of a four-link chain to the Frontend. DEFECT B6/B7 — the real distance exceeds the bar.
depends_on: ["[[Tier One]]"]
tags: [service, relay]
---

# Edge Relay

DEFECT B6/B7 (hop bar exceeded): the shortest path to the [[Frontend]] runs Edge Relay → Tier
One → Tier Two → Tier Three → Frontend, i.e. FOUR hops against a declared bar of three. A
`query` issued at `--depth max_hops` cannot see this — the target is simply absent from the
result and gets mis-reported as unreachable. Probing beyond the bar finds it and then fails it.
