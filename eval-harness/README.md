# codemap navigation eval

The packaged, deterministic form of the product bar (ARCHITECTURE §6.4 / the spec's Appendix-A
"served map answers a navigation question in ≤ 3 graph hops"). It proves the **serving** layer
makes curated cross-service knowledge navigable — a fast index to the right subsystem.

## Run

```bash
python3 navigation-eval.py                 # against the bundled fixture-vault
python3 navigation-eval.py --vault /path/to/your/wiki-vault
```

Exit 0 iff every question in `questions.json` resolves within its `max_hops` (≤ 3). Needs a working
`fmg` (on `PATH`, or `../bin/fmg`, or `../bin/fmg-<os>-<arch>` — `install.sh` provides it). Stdlib
only; no network, no LLM.

## What's here

- `navigation-eval.py` — the runner. Each question is answered by a served-map query — a coarse
  `query`/`bridge` traversal or a typed `xedges` lookup — **never** by reading source.
- `questions.json` — the question set. Covers all three runtime edge families (`http-call`,
  `data-store`, `shares-datastore`) plus a coarse `bridge` and a coarse `query`.
- `fixture-vault/` — a small, **anonymized, self-contained** model of an Appendix-A-shaped platform
  (Frontend, Backend, Context Service, Scheduler, a downstream MCP server, two data jobs, MongoDB,
  Redis) with coarse `[[WikiLink]]` frontmatter edges + typed `cross_service:` runtime edges + a
  `.fmg.toml`. It is a fixture — not tuned per question — so the eval is a fair test of navigation.

## Honest bound (from §6.4)

The map's speed is partly *pre-distilled knowledge*: the runtime edges are the answers, and `xedges`
reads them off. That is the value proposition, not a cheat — but the served map is a **fast index to
the right subsystem, not a substitute for verifying specifics against code**.
