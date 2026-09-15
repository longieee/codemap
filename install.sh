#!/usr/bin/env bash
# codemap — install script. Installs ALL dependencies for the navigation-map package.
# Idempotent. `./install.sh` installs; `./install.sh --check` ONLY verifies (preflight:
# no install, no file mutation, no network fetch).
#
# Dependencies (see docs/ARCHITECTURE.md §3.1 for why each):
#   1. fmg           runtime-edge store + MCP server (the Serve layer)
#   2. uv/uvx        runner for Serena
#   3. serena        LSP grounding oracle (stitcher false-positive grounding)
#   4. LSP backends  language servers Serena drives (pyright, typescript-language-server)
#   5. python venv   the stitcher (derive/ground/emit/infra/datastore/reconcile)
#   6. MCP register  wire `fmg serve` into the host agent tool (Claude Code / opencode / Cursor)
#   7. .fmg.toml     field/direction config for the served vault
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${CODEMAP_PREFIX:-$HOME/.local/bin}"
LOCK="$HERE/.install-lock"
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1

# --- pinned versions / refs (supply-chain: AGENTS §2a — no unpinned launchers) ---
FMG_REF="${FMG_REF:-master}"                       # fmg git ref for the cargo source-build fallback
# The fmg SOURCE REPO is instance config, not pack content: this pack is vendored into consumer
# workspaces, so it must not carry a vendor/personal repo URL (client-boundary rule — see
# docs/packaging-contract.md criterion 22). Resolved at the point of use, in this order:
#   $FMG_GIT  →  `fmg_git = "..."` in config/codemap.toml  →  unset, and the source-build
# fallback then REFUSES with an instruction instead of cloning a guessed repo.
FMG_GIT="${FMG_GIT:-}"                             # see config/codemap.toml.example → fmg_git
SERENA_VER="${SERENA_VER:-1.6.1}"                  # pinned Serena release
SERENA_PKG="serena-agent==${SERENA_VER}"
PYRIGHT_VER="${PYRIGHT_VER:-1.1.401}"
TSLS_VER="${TSLS_VER:-4.3.3}"
TYPESCRIPT_VER="${TYPESCRIPT_VER:-5.4.5}"

PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
VENDORED="$HERE/bin/fmg-$PLATFORM"                 # tracked, per-platform vendored binary
NEUTRAL="$HERE/bin/fmg"                            # install-materialized copy the manifest references

say(){ printf '\033[1;36m[codemap]\033[0m %s\n' "$*"; }
ok(){  printf '  \033[1;32mok\033[0m   %s\n' "$*"; }
bad(){ printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAILED=1; }
have(){ command -v "$1" >/dev/null 2>&1; }

FAILED=0

# tiny TOML value reader (key = "val") — good enough for flat config
codemap_cfg(){ local k="$1" d="${2:-}" f="$HERE/config/codemap.toml"
  if [ -f "$f" ]; then grep -E "^\s*$k\s*=" "$f" | head -1 | sed -E 's/^[^=]*=\s*"?([^"]*)"?.*/\1/'; else printf '%s' "$d"; fi; }

# ---------- 1. fmg (runtime-edge store + MCP server) ----------
install_fmg(){
  if [ $CHECK_ONLY -eq 1 ]; then
    if have fmg; then ok "fmg on PATH ($(fmg --version 2>/dev/null || echo '?'))"
    elif [ -x "$NEUTRAL" ]; then ok "fmg materialized at bin/fmg"
    elif [ -x "$VENDORED" ]; then ok "fmg vendored for $PLATFORM (not yet materialized — run install)"
    else bad "fmg absent (no PATH binary, no bin/fmg, no vendored bin/fmg-$PLATFORM)"; fi
    return
  fi
  # Materialize the platform-neutral bin/fmg that .mcp.json references
  # (${CLAUDE_PLUGIN_ROOT}/bin/fmg), and put fmg on PATH at $PREFIX.
  if [ -x "$VENDORED" ]; then
    # bin/fmg ships as a symlink → the linux binary; rm before copy so a non-linux
    # install writes a real per-platform binary instead of clobbering the vendored one.
    rm -f "$NEUTRAL"; cp "$VENDORED" "$NEUTRAL"; chmod +x "$NEUTRAL"
    mkdir -p "$PREFIX"; cp "$VENDORED" "$PREFIX/fmg"; chmod +x "$PREFIX/fmg"
    ok "fmg installed from vendored binary ($PLATFORM) → bin/fmg + $PREFIX/fmg"
  elif have cargo; then
    FMG_GIT="${FMG_GIT:-$(codemap_cfg fmg_git '')}"
    if [ -z "$FMG_GIT" ]; then
      bad "no vendored binary for $PLATFORM and no fmg source repo configured — set FMG_GIT=<fmg git url> or add fmg_git to config/codemap.toml (see config/codemap.toml.example), then re-run"
      return
    fi
    say "no vendored binary for $PLATFORM — building fmg from source ($FMG_GIT @ $FMG_REF) …"
    if cargo install --git "$FMG_GIT" --branch "$FMG_REF" --root "${PREFIX%/bin}" fmg; then
      rm -f "$NEUTRAL"; cp "$PREFIX/fmg" "$NEUTRAL" 2>/dev/null || true; chmod +x "$NEUTRAL" 2>/dev/null || true
      ok "fmg built from source + installed"
    else
      bad "cargo install fmg failed"
    fi
  elif have fmg; then
    ln -sf "$(command -v fmg)" "$NEUTRAL"; ok "linked bin/fmg → fmg already on PATH"
  else
    bad "fmg absent, no vendored binary for $PLATFORM, no cargo — install Rust or provide bin/fmg-$PLATFORM"
  fi
}

# ---------- 2. uv/uvx ----------
install_uv(){
  if have uvx; then ok "uv/uvx present"; return; fi
  [ $CHECK_ONLY -eq 1 ] && { bad "uvx absent"; return; }
  say "installing uv …"
  curl -LsSf https://astral.sh/uv/install.sh | sh && ok "uv installed" || bad "uv install failed"
}

# ---------- 3. Serena (LSP grounding oracle) — pinned ----------
install_serena(){
  if [ $CHECK_ONLY -eq 1 ]; then
    # offline check: is the pinned serena tool-installed? (never resolves over the network here)
    if uv tool list 2>/dev/null | grep -qi 'serena'; then ok "serena tool-installed ($SERENA_PKG)"
    else bad "serena not tool-installed ($SERENA_PKG) — run install"; fi
    return
  fi
  have uv || { bad "cannot install serena without uv"; return; }
  say "installing serena ($SERENA_PKG) …"
  uv tool install "$SERENA_PKG" >/dev/null 2>&1 \
    && ok "serena installed ($SERENA_PKG)" \
    || bad "serena install failed ($SERENA_PKG)"
}

# ---------- 4. LSP backends Serena drives — pinned ----------
install_lsp(){
  if have pyright-langserver || have pyright; then ok "pyright present"; else
    if [ $CHECK_ONLY -eq 1 ]; then bad "pyright absent"; else
      if have npm && npm i -g "pyright@$PYRIGHT_VER" >/dev/null 2>&1; then ok "pyright@$PYRIGHT_VER installed"; else bad "pyright: need node/npm"; fi
    fi
  fi
  if have typescript-language-server; then ok "typescript-language-server present"; else
    if [ $CHECK_ONLY -eq 1 ]; then bad "typescript-language-server absent"; else
      if have npm && npm i -g "typescript-language-server@$TSLS_VER" "typescript@$TYPESCRIPT_VER" >/dev/null 2>&1; then
        ok "typescript-language-server@$TSLS_VER installed"; else bad "tsls: need node/npm"; fi
    fi
  fi
  # Serena auto-fetches other language servers on first use.
}

# ---------- 5. python venv + stitcher deps ----------
install_py(){
  local venv="$HERE/.venv"
  if [ $CHECK_ONLY -eq 1 ]; then
    if [ -d "$venv" ] && "$venv/bin/python" -c "import yaml" 2>/dev/null; then ok "stitcher venv ready"
    else bad "stitcher venv missing or missing pyyaml"; fi
    return
  fi
  [ -d "$venv" ] || python3 -m venv "$venv"
  "$venv/bin/pip" install -q -r "$HERE/stitcher/requirements.txt" 2>/dev/null || true
  "$venv/bin/python" -c "import yaml" 2>/dev/null && ok "stitcher venv ready" || bad "stitcher deps missing (pyyaml)"
}

# ---------- 6. MCP registration (prints; no mutation) ----------
register_mcp(){
  local vault; vault="$(codemap_cfg vault_dir '${CLAUDE_PROJECT_DIR}')"
  say "MCP server = 'codemap' → command: fmg -w <vault> serve"
  cat <<EOF
  Claude Code : load this package as a plugin →  claude --plugin-dir "$HERE"
                (auto-registers 'codemap' from .claude-plugin/plugin.json + .mcp.json;
                 command = \${CLAUDE_PLUGIN_ROOT}/bin/fmg  -w \${CLAUDE_PROJECT_DIR} serve)
  opencode    : merge manifests/opencode.json  (set <CODEMAP_ROOT>=$HERE, <VAULT>=$vault)
  Cursor/other: merge manifests/cursor.mcp.json (same substitutions)
EOF
}

# ---------- 7. .fmg.toml in the vault ----------
install_fmgtoml(){
  local vault; vault="$(codemap_cfg vault_dir '')"
  if [ $CHECK_ONLY -eq 1 ]; then
    if [ -n "$vault" ] && [ -f "$vault/.fmg.toml" ]; then ok ".fmg.toml present in vault"
    else say ".fmg.toml not yet rendered (ok before first wiki-init)"; fi
    return
  fi
  if [ -n "$vault" ] && [ -d "$vault" ] && [ ! -f "$vault/.fmg.toml" ] && [ -f "$HERE/config/.fmg.toml.tmpl" ]; then
    cp "$HERE/config/.fmg.toml.tmpl" "$vault/.fmg.toml"; ok ".fmg.toml rendered into $vault"
  else
    say ".fmg.toml step skipped (no vault dir configured yet, or already present)"
  fi
}

main(){
  say "$([ $CHECK_ONLY -eq 1 ] && echo 'preflight check (no mutation, no network)' || echo 'installing dependencies')"
  install_fmg; install_uv; install_serena; install_lsp; install_py; install_fmgtoml; register_mcp
  echo
  if [ "$FAILED" -eq 0 ]; then
    say "all dependencies satisfied ✓"
    if [ $CHECK_ONLY -eq 0 ]; then
      printf 'codemap=1.0.0 fmg=%s serena=%s pyright=%s tsls=%s ts=%s\n' \
        "$(fmg --version 2>/dev/null || echo '?')" "$SERENA_VER" "$PYRIGHT_VER" "$TSLS_VER" "$TYPESCRIPT_VER" > "$LOCK"
      say "wrote $LOCK"
    fi
  else
    say "some dependencies missing — see FAIL lines above"; exit 1
  fi
}
main
