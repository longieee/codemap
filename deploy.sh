#!/usr/bin/env bash
# Deploy this codemap pack into a consumer workspace.
#
#   bash deploy.sh [--mode=copy|sync|link] [--check] <workspace>
#
# codemap is a PACK: one canonical tree, vendored into each workspace that uses it. This script is
# the only sanctioned way a copy is made, and it is the one file excluded from the copy it makes —
# a deployed copy therefore cannot act as a deploy source, which is the mechanical form of "a
# deployed copy has no authority" (README, "Canonical pack, vendored copies").
#
# Destination: <workspace>/_codemap/
#   The underscore marks the directory as PACK-MANAGED rather than project content, matching the
#   estate's other pack (whose copy lands at <workspace>/_harness). It was plain `codemap/` until
#   2026-09-15; a pack-managed directory that looks like project content invites exactly the
#   in-place edit this topology exists to prevent.
#
# What it does (idempotent; requires rsync, and python3 for the pre-write gate):
#   1. Runs the boundary gate on the tree about to be copied — BEFORE anything is written.
#   2. Copies the pack  -> <workspace>/_codemap/, excluding .git, this script, build residue and
#      the destination's own private working state.
#   3. Stamps the destination with PROVENANCE (what the copy is), and in sync mode PACK_SOURCE
#      (what it follows, so it can be refreshed).
#
# What it deliberately does NOT do: install anything. Dependencies are the destination's own
# `./install.sh` step, which needs a network and a host decision; deploying is a file operation.
set -euo pipefail

PACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_NAME="_codemap"

usage() {
  cat <<'USAGE'
usage: bash deploy.sh [--mode=copy|sync|link] [--check] <workspace>

  --mode=copy   (default) vendored copy, no --delete. Safe for a first deploy into a
                workspace that already holds files.
  --mode=sync   converge: adds --delete so the copy is exactly the canonical tree rather
                than the union of every tree ever deployed into it, and writes PACK_SOURCE.
                Private working state and the destination-only stamps stay excluded, so
                converging never removes them.
  --mode=link   no copy: <workspace>/_codemap is a symlink YOU create. This script detects
                it, runs in place, and skips both the copy and the stamp — identity comes
                from the canonical pack's own git. An existing symlink always wins over --mode.
  --check       dry run for any mode: prints what would change, creates nothing, stamps nothing.

Environment (identity overrides, for deploying from a `git archive` extraction that has no git):
  CODEMAP_PACK_MODE, CODEMAP_PACK_REPO, CODEMAP_PACK_SHA, CODEMAP_PACK_RELEASE,
  CODEMAP_PACK_TRACKED_REF, CODEMAP_PACK_SOURCE_PATH, CODEMAP_DEPLOY_SKIP_GUARD=1
USAGE
}

# --- arguments ------------------------------------------------------------------------------ #
MODE="${CODEMAP_PACK_MODE:-copy}"
CHECK=0
WORKSPACE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode=*)   MODE="${1#--mode=}"; shift ;;
    --mode)     shift; MODE="${1:?--mode needs a value: copy|sync|link}"; shift ;;
    --check|-n) CHECK=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    --)         shift; break ;;
    -*)         echo "deploy: unknown flag '$1' (see --help)" >&2; exit 64 ;;
    *)          if [ -n "$WORKSPACE" ]; then echo "deploy: unexpected extra argument '$1'" >&2; exit 64; fi
                WORKSPACE="$1"; shift ;;
  esac
done
case "$MODE" in copy|sync|link) : ;; *) echo "deploy: unknown --mode '$MODE' (copy|sync|link)" >&2; exit 64 ;; esac
if [ -z "$WORKSPACE" ]; then usage >&2; exit 64; fi
[ -d "$WORKSPACE" ] || { echo "deploy: workspace '$WORKSPACE' is not a directory" >&2; exit 64; }
WORKSPACE="$(cd "$WORKSPACE" && pwd)"
DEST="$WORKSPACE/$DEST_NAME"

CHECK_LABEL=""
[ "$CHECK" = 1 ] && CHECK_LABEL="  [--check: dry run, nothing will be written]"
echo "codemap deploy: $PACK_DIR  ->  $DEST   (mode=$MODE)$CHECK_LABEL"

# --- 0. a deployed copy is not a deploy source ---------------------------------------------- #
# The documented rule (README: "A deployed copy has no authority, cannot be a deploy source") is
# normally enforced mechanically, because this script is excluded from the copy. It stops being
# mechanical the moment someone copies the tree by hand or promotes a deployment to canonical —
# which is how this file came to be missing in the first place. So the rule is also asserted here.
#
# The discriminator is the pair, not either half alone:
#   PROVENANCE present AND no git checkout  -> a vendored copy. Refuse.
#   PROVENANCE present AND a git checkout   -> a canonical pack carrying destination-only stamps.
#                                              That is a contract violation of its own (the stamps
#                                              belong to a destination), but the copy this run
#                                              makes is unharmed because they are excluded from
#                                              the rsync. Warn loudly and continue, so the warning
#                                              cannot be mistaken for a clean run.
PACK_IS_GIT_CHECKOUT=0
[ -e "$PACK_DIR/.git" ] && PACK_IS_GIT_CHECKOUT=1
PACK_HAS_STAMP=0
for _s in PROVENANCE PACK_SOURCE; do
  [ -f "$PACK_DIR/$_s" ] && PACK_HAS_STAMP=1
done
if [ "$PACK_HAS_STAMP" = 1 ] && [ "$PACK_IS_GIT_CHECKOUT" = 0 ]; then
  echo "deploy: REFUSED — $PACK_DIR carries a destination-only stamp and is not a git checkout," >&2
  echo "        so it is itself a deployed copy. A deployed copy has no authority and cannot be a" >&2
  echo "        deploy source; deploy from the canonical pack instead." >&2
  exit 4
fi
if [ "$PACK_HAS_STAMP" = 1 ]; then
  echo "  ⚠ this canonical pack carries a destination-only stamp (PROVENANCE/PACK_SOURCE) — those" >&2
  echo "    belong to a DESTINATION only (docs/packaging-contract.md criterion 23). They are" >&2
  echo "    excluded from this copy, so the destination is unaffected, but the pack needs cleaning." >&2
fi

# --- 1. boundary gate, BEFORE anything is written ------------------------------------------- #
# Deploying into a consumer workspace IS publishing, so the checks that guard publication run on
# the tree about to be copied: criterion 22 (no client/host/person name in any shipped text file)
# and criterion 8 (no host-specific path in any shipped text file). Both are invoked from the
# pack's own acceptance suite rather than reimplemented here, so a rule added there is enforced
# here with no second copy to keep in step.
#
# A gate that cannot run must never look like a gate that passed: no python3, or an import that
# fails, is a REFUSAL, not a pass. CODEMAP_DEPLOY_SKIP_GUARD=1 skips explicitly and says so.
if [ "${CODEMAP_DEPLOY_SKIP_GUARD:-0}" = "1" ]; then
  echo "  ⚠ boundary gate SKIPPED by CODEMAP_DEPLOY_SKIP_GUARD=1 — this copy is NOT gated" >&2
elif ! command -v python3 >/dev/null 2>&1; then
  echo "deploy: REFUSED — no python3, so the boundary gate (criteria 22 + 8) could not run." >&2
  echo "        Deploying is publishing; an ungated copy is not a pass. Install python3, or set" >&2
  echo "        CODEMAP_DEPLOY_SKIP_GUARD=1 to accept an ungated copy deliberately." >&2
  exit 3
else
  if ( cd "$PACK_DIR" && python3 - <<'GATE'
import pathlib, sys
sys.path.insert(0, str(pathlib.Path("tests").resolve()))
import test_packaging as t
failures = []
for name in ("test_c22_no_client_boundary_names", "test_c8_no_host_specific_paths"):
    fn = getattr(t, name, None)
    if fn is None:
        failures.append("%s: not found in tests/test_packaging.py" % name)
        continue
    try:
        fn()
    except Exception as exc:
        failures.append("%s: %s" % (name, exc))
for f in failures:
    print("  " + f, file=sys.stderr)
sys.exit(1 if failures else 0)
GATE
  ); then
    echo "  ✓ boundary gate passed on the tree about to be copied (criteria 22 + 8)"
  else
    echo "deploy: REFUSED — the boundary gate found publish violations in $PACK_DIR." >&2
    echo "        Fix them in the canonical pack (never in a copy), then deploy." >&2
    exit 3
  fi
fi

# --- 2. exclusions -------------------------------------------------------------------------- #
# Private working state belongs to the WORKSPACE, not to the pack. `rsync -a` honours neither
# .gitignore nor "don't clobber", so without this list a re-deploy would overwrite a workspace's
# real configuration with the pack's generic absence of one, and sync mode's --delete would remove
# it outright. If you add a new kind of per-instance file, add it here in the same change (README,
# "Private instance state").
PRIVATE_WORKING_STATE=(config/codemap.toml .install-lock .venv "*.candidates.json" .harness-memory)

# Written into a DESTINATION, never present in the pack. Excluded from the copy so a re-deploy
# cannot clobber a destination's own answer to "what am I".
DEST_ONLY_STAMPS=(PROVENANCE PACK_SOURCE)

# Excluded in EVERY mode, not just in sync. The narrower form — excluding them only alongside
# sync's --delete, on the reasoning that the source never carries a stamp and the exclude exists
# only to stop --delete removing the destination's — held until a deployment was moved in to
# become the canonical pack. Then the source DID carry stamps, and a copy-mode deploy off that
# tree copied a stale PACK_SOURCE into the destination verbatim: the copy then advertised itself
# as refreshable from a pack path and ref that were not its own. Observed 2026-09-15 on a copy-mode
# deploy from the real canonical checkout — the only case that reproduces it, because it is the one
# where the SOURCE carries stamps; the same run against a fixture with the stamps removed was green,
# which is exactly how narrow this blind spot is. PROVENANCE was overwritten by the fresh stamp and
# so hid the same bug.
# Excluding them unconditionally costs nothing and does not weaken sync: rsync does not delete an
# excluded path unless --delete-excluded is given, so a destination's own stamps still survive a
# converge.
STAMP_EXCLUDES=()
for s in "${DEST_ONLY_STAMPS[@]}"; do STAMP_EXCLUDES+=(--exclude "/$s"); done

# .git: a copy is not a checkout. /deploy.sh: rooted, so the copy cannot be a deploy source.
# __pycache__/, *.pyc, .pytest_cache/: build residue, regenerated on first use in the destination.
RSYNC_EXCLUDES=(--exclude ".git" --exclude "/deploy.sh" --exclude "__pycache__/" --exclude "*.pyc" --exclude ".pytest_cache/" "${STAMP_EXCLUDES[@]}")
for p in "${PRIVATE_WORKING_STATE[@]}"; do
  case "$p" in
  /*|*/*) RSYNC_EXCLUDES+=(--exclude "/$p") ;;   # rooted path: only that path
  \**)    RSYNC_EXCLUDES+=(--exclude "$p") ;;    # glob: anywhere in the tree
  *)      RSYNC_EXCLUDES+=(--exclude "$p") ;;    # bare name: anywhere in the tree
  esac
done
# Sync mode CONVERGES: without --delete the copy is the union of every tree ever deployed into it,
# so a file a release removed lingers and the copy is only approximately the pinned tree — which is
# exactly the attribution sync mode exists to provide. The stamp excludes are already in the base
# list above (and are what stops --delete removing the destination's own stamps).
if [ "$MODE" = sync ]; then
  RSYNC_EXCLUDES+=(--delete)
fi

# --- 3. the copy ---------------------------------------------------------------------------- #
# A symlinked destination is the documented no-copy mode: the workspace runs the pack in place, so
# there is nothing to vendor and NOTHING TO STAMP — identity comes from the pack's own git. Without
# this guard both the rsync and the PROVENANCE write would go THROUGH the symlink into the
# canonical checkout itself, mislabelling it as a vendored copy. Copy and stamp are skipped
# together: they are two halves of "vendoring". An existing symlink always wins over --mode.
DEST_IS_SYMLINK=0
[ -L "$DEST" ] && DEST_IS_SYMLINK=1
if [ "$MODE" = link ] && [ "$DEST_IS_SYMLINK" = 0 ]; then
  echo "  • --mode=link but $DEST is not a symlink — create it yourself, then re-run:" >&2
  echo "      ln -s <relative-path-to-canonical-pack> $DEST" >&2
  exit 64
fi
if [ "$DEST_IS_SYMLINK" = 1 ]; then
  echo "  • $DEST is a symlink ($(readlink "$DEST")) — pack copy and PROVENANCE stamp skipped (runs in place; identity from the pack's own git)"
fi

# Resolve the tracked ref BEFORE the stamp, so a synced copy records what it FOLLOWS as well as
# what it currently is. An existing PACK_SOURCE wins over the default: re-deploying must not
# silently move a workspace that was deliberately pinned to a ref.
SYNC_TRACKED_REF=""
if [ "$MODE" = sync ]; then
  SYNC_TRACKED_REF="${CODEMAP_PACK_TRACKED_REF:-}"
  if [ -z "$SYNC_TRACKED_REF" ] && [ -f "$DEST/PACK_SOURCE" ]; then
    SYNC_TRACKED_REF="$(sed -n 's/^tracked_ref=//p' "$DEST/PACK_SOURCE" | head -n1)"
  fi
  SYNC_TRACKED_REF="${SYNC_TRACKED_REF:-release}"
fi

if [ "$DEST_IS_SYMLINK" = 0 ]; then
  if [ "$CHECK" = 1 ]; then
    # Dry run: -n so rsync writes nothing, -i so every planned change is itemized. The destination
    # is deliberately NOT created — "--check creates nothing" includes the directory itself, and a
    # --check that mkdir'd its target would leave a trace of a run that was supposed to leave none.
    [ -d "$DEST" ] || echo "  • would create $DEST"
    echo "  • would copy (rsync --dry-run; '>' = content, 'c' = create, '*deleting' = converge):"
    # rsync's own dry-run narration includes a bare "created directory <dest>" line, which in a
    # --check whose contract is "creates nothing" reads as a report that it DID create it. Dropped
    # here, and replaced by the "would create" line above, so the dry run cannot be misread as a run.
    rsync -a -n -i "${RSYNC_EXCLUDES[@]}" "$PACK_DIR/" "$DEST/" \
      | grep -v '^created directory ' | sed 's/^/      /' || true
    echo "  • would stamp $DEST_NAME/PROVENANCE (destination-only)"
    [ "$MODE" = sync ] && echo "  • would stamp $DEST_NAME/PACK_SOURCE (tracked_ref=$SYNC_TRACKED_REF)"
    echo "  • excluded: private working state ${PRIVATE_WORKING_STATE[*]}; stamps ${DEST_ONLY_STAMPS[*]}; .git; /deploy.sh; build residue"
  else
    mkdir -p "$DEST"
    rsync -a "${RSYNC_EXCLUDES[@]}" "$PACK_DIR/" "$DEST/"
    echo "  ✓ $DEST_NAME/ pack copied (excluded: private working state ${PRIVATE_WORKING_STATE[*]}; stamps ${DEST_ONLY_STAMPS[*]}; .git; /deploy.sh; build residue)"
  fi
fi

# --- 4. destination-only stamps ------------------------------------------------------------- #
# Every vendored copy must be able to answer "what am I" with the CANONICAL pack's identity, not
# the identity of whatever repo the copy happens to sit in — so two byte-identical copies in
# different workspaces report the same thing. The CODEMAP_PACK_* overrides exist because a deploy
# from a `git archive` extraction of a tagged tree has no git, and without them such a copy would
# stamp itself 'unknown' and lose exactly the attribution sync mode is for.
if [ "$DEST_IS_SYMLINK" = 0 ] && [ "$CHECK" = 0 ]; then
  {
    echo "canonical_repo=${CODEMAP_PACK_REPO:-$(git -C "$PACK_DIR" remote get-url origin 2>/dev/null || echo 'unknown')}"
    echo "canonical_sha=${CODEMAP_PACK_SHA:-$(git -C "$PACK_DIR" log -1 --format=%h 2>/dev/null || echo 'unknown')}"
    echo "pack_version=$(cat "$PACK_DIR/VERSION" 2>/dev/null || echo 'unversioned')"
    echo "pack_release=${CODEMAP_PACK_RELEASE:-$(git -C "$PACK_DIR" describe --tags --exact-match HEAD 2>/dev/null || echo 'none')}"
    echo "tracked_ref=${CODEMAP_PACK_TRACKED_REF:-${SYNC_TRACKED_REF:-none}}"
    echo "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$DEST/PROVENANCE"
  echo "  ✓ PROVENANCE stamped ($(sed -n 's/^canonical_sha=//p' "$DEST/PROVENANCE") @ $(sed -n 's/^pack_version=//p' "$DEST/PROVENANCE"))"

  # PACK_SOURCE is what makes a copy REFRESHABLE: it names the canonical checkout and the ref the
  # workspace tracks. Its absence is how a one-off copy is told from a sync-mode workspace. It
  # records a local filesystem path, which is why copy mode does not write one — prefer copy mode
  # for a copy that lives in a client-facing workspace.
  if [ "$MODE" = sync ]; then
    SRC_PATH="${CODEMAP_PACK_SOURCE_PATH:-$PACK_DIR}"
    [ -d "$SRC_PATH" ] && SRC_PATH="$(cd "$SRC_PATH" && pwd)"
    {
      echo "pack_path=$SRC_PATH"
      echo "tracked_ref=$SYNC_TRACKED_REF"
      echo "written_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$DEST/PACK_SOURCE"
    echo "  ✓ sync mode — PACK_SOURCE written (tracked_ref=$SYNC_TRACKED_REF)"
  fi
fi

echo
if [ "$CHECK" = 1 ]; then
  echo "Dry run only — nothing was created, copied or stamped."
else
  echo "Done. In the destination:"
  echo "  1. cp $DEST_NAME/config/codemap.toml.example $DEST_NAME/config/codemap.toml  — point it at this workspace's repos + vault"
  echo "  2. cd $DEST_NAME && ./install.sh --check   — preflight the seven dependencies (mutates nothing)"
  echo "  3. cd $DEST_NAME && ./install.sh           — install + pin; writes .install-lock"
  echo "  Edit the canonical pack and re-deploy — never edit this copy."
fi
