# init/

The **cold-start backbone** — how a wiki that does not exist yet gets its page inventory.

`inventory.py` surveys a workspace and enumerates every page that should exist *up front*, so
coverage is an explicit, inspectable decision instead of whatever a long generation run happened to
remember. It is budget-aware and defers rather than drops: a page that does not fit the current
budget is recorded as deferred with a reason, not silently skipped.

The `wiki-init` skill orchestrates around this file (survey -> inventory -> bounded per-page
workers -> verify loop); the interface it must satisfy is `docs/cold-start-contract.md`, and the
tests derived from that contract are `tests/test_inventory.py`.
