# skills/wiki-tools/

**Query skill**: how to ask the served map a question instead of reading source.

`SKILL.md` documents the `fmg` command surface — `describe`, `query` (multi-hop traversal with
fuzzy node matching), `bridge` (shortest path), `centrality` (hub discovery), and `xedges` (typed
cross-service runtime edges with endpoint, enabling condition and `repo/path:line` provenance) —
plus semantic/keyword search over the wiki's indexed content and the instance-configured path to
repo source when enrichment needs it.

Read this before answering a "who calls whose endpoint, under what condition" question by hand:
the whole point of the pack is that the map answers it in a bounded number of hops.
