# skills/wiki-maintainer/

**Maintenance skill**: keep an existing wiki's pages and edges true as the codebase moves.

`SKILL.md` covers page anatomy and frontmatter fields, the cross-linking rules (`depends_on`,
`part_of`, `owned_by`, `related_to`, `supersedes`, `documented_by`), the per-page minimums, and the
review loop for assessing page quality.

It also documents the one rule that protects derived data: `cross_service:` blocks are
**machine-written only**, by the write door (`../../stitcher/write_door.py`) — minimal-diff,
idempotent, and validated against real target pages. Hand-editing them is how derived edges and
their provenance get silently lost.
