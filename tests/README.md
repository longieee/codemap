# tests/

**Contract-derived acceptance tests.** Each test file is derived from a document in `../docs/` and
from nothing else — never from the implementation it checks — so a test going red means the package
drifted from its contract, not that a refactor moved code:

- `test_packaging.py` <- `docs/packaging-contract.md` (pack shape, manifest, skills,
  self-containment, vendored binary, `install.sh`, eval harness, referential integrity, and the
  client boundary)
- `test_inventory.py` <- `docs/cold-start-contract.md`
- `test_envdiff.py` <- `docs/envdiff-contract.md`
- `test_tier_a_widen.py` <- `docs/tier-a-widen-contract.md`

Stdlib only, no pytest required: `python3 tests/test_packaging.py` prints ok/FAIL/skip per
criterion and exits non-zero on any failure (`pytest` still works if it is installed).

Two conventions worth knowing before editing a test here. Criteria that need a working `fmg`
**skip** with a stated reason rather than passing quietly. And the client-boundary check
(criterion 22) is the one place in the pack allowed to carry real client-name literals — they are
its detector — each on a line marked `boundary-allow`; the marker is per line, never per file, and
the check fails if its own pattern ever stops matching anything.
