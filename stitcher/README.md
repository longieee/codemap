# stitcher/

The **owned core** — everything that turns code and infrastructure into typed edges. Three layers,
each blind to what the others see, which is why all three exist:

- `derive.py` — logical layer: outbound HTTP/RPC call sites in application code, with the endpoint,
  the enabling condition and `repo/path:line` provenance.
- `infra.py` — physical layer: IaC wiring (invoke/pubsub/deploy-env) between deployed services.
- `datastore.py` — shared-datastore layer: two services coupled only because one writes a
  collection the other reads, with no HTTP surface between them at all.

Supporting modules: `ground.py` (LSP grounding as false-positive control), `reconcile.py` (static
IaC vs a live cloud snapshot), `envdiff.py` (one environment's reconciled map vs another's),
`emit.py` (edges -> `cross_service:` patches + service inventory), `freshness.py` (the
`extracted_from` stamp every derived record carries) and `write_door.py` (the only sanctioned
writer of `cross_service:` frontmatter: minimal-diff, idempotent, validated).

## Run it

```bash
./stitcher/run-pipeline.sh --config config/codemap.toml --out-dir build/
```

One command runs **all three families** and reports per-family edge counts, warning when a family
produced nothing. Use it rather than invoking the modules by hand: the five CLIs were previously
sequenced by a human, and the physical layer went entirely unemitted for a full milestone because
two of the five were simply never run and nothing reported their absence.

One command is **nine steps**. With `--vault <dir>` it also generates the physical-layer node
pages (7/9), merges every family through the write door (8/9) and gives every remaining off-vault
edge target a stub page (9/9). Step 9 runs last because it reads the SERVED vault, and it runs even
when the write door exits non-zero — which on a real workspace it does, by design — with the
failing step named in the exit line. `--skip-stubs` opts out and says what that costs.

`--live <topology.json>` reconciles against a cloud snapshot (Tier B/C); without it the static-only
reconcile still yields every IaC-declared physical edge. `--allow-ungrounded` proceeds when
`[stitcher] ground = true` but the oracle is unavailable — deliberate, never a default; the runner
exits non-zero instead of publishing unvalidated edges that look validated. `--env <name>` renders
IaC names for one `[cloud.environments.<name>]`, whose `tfvars` table supplies values for variables
that have no default; with no `--env` no var-file is read and such variables stay
`(unset:var.<name>)` (contract C13.4).

Contracts: `docs/logical-layer-contract.md` (derivation, grounding gate, freshness),
`docs/tier-a-widen-contract.md` (static IaC), `docs/envdiff-contract.md` (cross-environment diff).
Tests: `tests/test_logical_layer.py`, `tests/test_tier_a_widen.py`, `tests/test_envdiff.py`.

Nothing here hardcodes an instance's names: repos, languages and service hints all come from
`config/codemap.toml`. Requirements for the venv `install.sh` builds are in `requirements.txt`.
