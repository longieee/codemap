# config/

Configuration for one **instance** of codemap: which repos to scan, where the served vault lives,
which extractors run, and which cloud environments to discover.

- `codemap.toml.example` — the template that ships. It is deliberately generic: logical repo
  names (`frontend`, `context-svc`), placeholder workspace paths, commented-out hints.
- `.fmg.toml.tmpl` — the graph-field/direction config `install.sh` writes into the served vault so
  `fmg` knows which frontmatter fields are edges and which way they point.
- `codemap.toml` — **private instance state, never pack content.** It names real repo paths and
  service names, so it is git-ignored, excluded from the canonical pack, and excluded from every
  deploy (see `../deploy.sh`, `PRIVATE_WORKING_STATE`). A re-deploy must never overwrite it.

A fresh `deploy.sh` seeds `codemap.toml` from the example if none exists, then leaves it alone
forever. Point it at the instance's workspace and vault before running `install.sh`.
