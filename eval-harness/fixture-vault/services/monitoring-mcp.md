---
title: Monitoring MCP Server
type: service
status: active
summary: Downstream MCP server the Frontend dispatches tool calls to (no local source).
related_to: ["[[Frontend]]"]
tags: [service, mcp, downstream]
---

# Monitoring MCP Server

A downstream MCP server. It has **no local source checkout** — it is a first-class map node so the
Frontend's tool-call fan-out has a real target. Reached over MCP from the [[Frontend]].
