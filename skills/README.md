# skills/

The three **bundled agent skills** — the operator-facing half of the pack. The stitcher derives
edges and `fmg` serves them; these skills are how an agent is told to build, maintain and query
the map.

- `wiki-init/` — cold-start a wiki that does not exist yet.
- `wiki-maintainer/` — keep pages and their frontmatter edges correct as the codebase moves.
- `wiki-tools/` — query the served map (`fmg query` / `bridge` / `xedges`, semantic search).

Exactly these three, no more and no fewer, each with a `SKILL.md` whose frontmatter carries a
non-empty `name` and `description` (`docs/packaging-contract.md` criteria 5-7). The skills are
sanitized copies: no instance secret plumbing and no client names (criteria 9 and 22), so their
examples use the same neutral vocabulary as the eval fixture vault.
