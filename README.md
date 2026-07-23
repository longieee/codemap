# codemap

Builds and serves a **navigation map of a multi-repo codebase** — including the cross-service
runtime edges (who calls whose endpoint, under what condition, at which call site; who shares which
datastore; how services are wired on the cloud) that no code-nav, telemetry, or wiki tool produces —
over a prose wiki, served via MCP. Drops into any project via one install script.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the problem, the design, and the measured
results.

## Install (one command)

```bash
./install.sh            # installs + pins ALL deps; writes .install-lock
./install.sh --check    # preflight only — verifies, mutates nothing, no network
```

Installs: the **fmg** graph store/MCP server (vendored static binary → `bin/`, or built from source
via `cargo`), **uv** + pinned **Serena** (the LSP grounding oracle) + language servers, a Python
**venv** for the stitcher, **MCP registration**, and the vault's `.fmg.toml`. See `install.sh` for
the seven dependencies and their pins.

## Use it

1. Point [`config/codemap.toml`](config/codemap.toml.example) at your repos + served vault
   (copy `config/codemap.toml.example` → `config/codemap.toml`).
2. **Cold-start** a wiki with the `wiki-init` skill (survey → page-inventory backbone
   [`init/inventory.py`](init/inventory.py) → bounded per-page workers → verify loop).
3. **Stitch** cross-service edges with the owned core in [`stitcher/`](stitcher/) and merge them via
   the maintainer's validated write door.
4. **Serve + query** the map: load the Claude Code plugin, then ask via the `codemap` MCP server —
   or run `fmg -w <vault> <query|bridge|xedges|centrality|...>` directly. See the `wiki-tools` skill.

### Register with an agent tool

| Host | How |
|------|-----|
| Claude Code | `claude --plugin-dir <path-to-codemap>` (auto-loads `.claude-plugin/plugin.json` + `.mcp.json`) |
| opencode / Cursor | merge the shim from [`manifests/`](manifests/) |

## Layout

`.claude-plugin/` + `.mcp.json` (Claude Code manifest) · `manifests/` (opencode/Cursor shims) ·
`bin/` (vendored `fmg`) · `stitcher/` (owned core) · `init/` (cold-start backbone) ·
`discovery/` (read-only cloud discovery) · `skills/` (3 bundled skills) · `config/` ·
`eval-harness/` (navigation eval + fixture vault) · `tests/` (contract-derived acceptance tests) ·
`docs/`.

## Verify

```bash
python3 tests/test_packaging.py           # packaging acceptance tests (contract-derived)
python3 eval-harness/navigation-eval.py   # served map answers each nav question in <= 3 hops
```
