# docs/

The pack's **contracts and design prose**. Two kinds of document, and the distinction is
load-bearing:

- **Contracts** (`packaging-contract.md`, `cold-start-contract.md`, `envdiff-contract.md`,
  `tier-a-widen-contract.md`) — human/implementer-owned interface specifications. The acceptance
  tests under `tests/` are derived from these documents and from nothing else; a change to the
  package's *shape* is a change to the contract, reviewed there first, and only then made green in
  the implementation. Each numbered criterion maps to a `test_*` function.
- **Design prose** (`cloud-discovery.md`, plus `../ARCHITECTURE.md` at the pack root) — the
  problem, the layer model, the pipeline, and what the design cannot see.

Docs ship with the pack, so they are inside the client-boundary scan (criterion 22): examples use
neutral stand-ins, not real client, repo or host names.
