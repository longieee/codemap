# `fmg/` — source changes and gates for the vendored graph store

`bin/fmg-<platform-tag>` is a compiled binary. On its own it is not reviewable: you cannot read
what it does, and you cannot check any claim made about it. This directory closes that gap. It
carries the exact source changes the vendored binary was built from, and the gate scripts that
produced the numbers reported about it, so a reviewer with only this pack can rebuild the binary
and re-run every check.

Nothing here is needed at runtime. It is evidence.

## Contents

| path | what it is |
|---|---|
| `fmg-graph-store.patch` | the graph-store changes, as a diff against upstream `fmg` at `2cf3471` |
| `gates/parity.sh` | binary-vs-binary output parity across every subcommand and format |
| `gates/gate.py` | the served edge record projects what the page carries; scan exclude honoured |
| `gates/diffgate.py` | store and `tools/lint.py` grade the same page set for the same `[scan] exclude` |
| `gates/dyngate.py` | `dynamic` candidates are distinguishable and filterable |

## Rebuilding the binary

The build is **bit-reproducible**: applying the patch to a clean checkout and building with the
flags below produces a binary whose `sha256` equals the vendored one. That equality is the check —
if it does not hold, something in this directory does not describe what ships.

The upstream repository URL is **instance config, not pack content** — the same convention
`install.sh` uses for its cargo fallback (`install.sh:22-28`). Take it from `$FMG_GIT`, or from
`fmg_git` in `config/codemap.toml`; this pack deliberately does not carry it.

```sh
git clone "$FMG_GIT" src && cd src
git checkout 2cf3471
git apply /path/to/pack/fmg/fmg-graph-store.patch
cargo test --release                     # expect: 26 passed

# Static musl, so the binary runs on any Linux regardless of host libc.
# --remap-path-prefix is load-bearing, not cosmetic: without it every dependency's
# panic-location string carries the builder's home directory into a client deliverable.
export RUSTFLAGS="-C strip=symbols --remap-path-prefix=$HOME=/build --remap-path-prefix=$PWD=/fmg"
cargo build --release --target x86_64-unknown-linux-musl

sha256sum target/x86_64-unknown-linux-musl/release/fmg   # compare with bin/fmg-<platform-tag>
```

**Toolchain note.** The musl standard-library target is not on conda-forge. If
`cargo build --target x86_64-unknown-linux-musl` reports a missing std, install it with `rustup
target add x86_64-unknown-linux-musl`, or — on a toolchain without rustup — fetch
`rust-std-<version>-x86_64-unknown-linux-musl` from the official Rust distribution, overlay it
onto a copy of the toolchain's `lib/rustlib/`, and pass that copy as `--sysroot` in `RUSTFLAGS`.

**Only the `x86_64` Linux target is built and vendored.** `bin/` ships one platform tag; any other
platform resolves through `install.sh`'s cargo fallback, which builds upstream `fmg` from `$FMG_GIT` — i.e.
the behaviour *without* these changes — until the patch is merged upstream.

## Running the gates

Each gate prints one line per check with an explicit three- or five-valued outcome, and
distinguishes "this binary cannot do it" from "this vault cannot exercise it". A check that could
not be run is never counted as a pass.

```sh
PACK=/path/to/pack
BIN=$PACK/bin/fmg-linux-x86_64

# 1. Parity — candidate vs baseline binary on one vault.
#    Outcomes: IDENTICAL | DIFF | CHANGED-A-EMPTY | CHANGED-B-EMPTY | NOT_EXERCISED.
$PACK/fmg/gates/parity.sh <baseline-binary> "$BIN" "$PACK/eval-harness/field-probe-vault" probe

# 2. Edge record + scan exclude. The second vault must be a copy whose .fmg.toml
#    sets `[scan] exclude = []`, which is how the gate proves the value is read from
#    config rather than hardcoded.
python3 $PACK/fmg/gates/gate.py "$BIN" <vault> <vault-with-exclude-disabled> label

# 3. Store vs lint agreement on [scan] exclude, over a pattern matrix.
python3 $PACK/fmg/gates/diffgate.py "$BIN" <vault>

# 4. Dynamic-class serving and filtering.
python3 $PACK/fmg/gates/dyngate.py "$BIN" <vault-containing-dynamic-edges> label
```

`gates/diffgate.py` imports `tools/lint.py` from this pack by resolving the pack root from its own
location, so it works from any checkout without editing a path.

Use `eval-harness/field-probe-vault` where a gate needs a vault carrying every emitted field: it
is built to carry one edge of every family, so a failure there is a property of the store and not
of some particular vault's content.

## What a green gate does and does not establish

Every check in `gates/` has been shown to fail on a deliberately seeded defect before being
trusted — a check that has only ever been green is not yet evidence. Two limits are worth stating
plainly:

- A gate run against a vault that does not carry the field under test reports `NOT_EXERCISED`, not
  a pass. Read those rows; they are the ones that say your evidence is missing.
- `parity.sh` compares two binaries' output. It establishes that behaviour did or did not change.
  It says nothing about whether the behaviour is correct — that is what the other three gates and
  the eval harness are for.