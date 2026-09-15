---
title: Report Net
type: infrastructure
status: active
summary: VPC connector the Report Gateway egresses through. Retained as a node; no longer an edge target.
related_to: ["[[Report Gateway]]"]
tags: [infrastructure, retired-family]
---

# Report Net

VPC connector. This page was the target of a `network` physical edge until the physical-edge
vocabulary was pinned to the six families `stitcher/infra.py` actually emits — `invokes`,
`subscribes-to`, `deploy-env`, `runs-as`, `in-dataset`, `reads-from`. `network` is not among
them and never was: the emitter produces `network-vpc`, `network-subnet` and
`network-connector` as node **kinds** (`infra.py:395,398,404`), never as an edge type. The
[[Report Gateway]]'s edge was retyped to `reads-from` and the connector kept as a plain node,
which is what the emitter would produce for it.

The page is kept rather than deleted for two reasons. It is the fixture's record of a name that
was asserted for a full round and measured nothing — the self-confirming property in its purest
form, worth leaving visible. And a fixture page is graph content: removing one silently changes
the graph every question is measured against, so a retired page is retired in place.
