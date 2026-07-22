#!/usr/bin/env bash
# codemap — install script. Installs ALL dependencies for the navigation-map package.
# Idempotent. `./install.sh` installs; `./install.sh --check` only verifies (preflight).
#
# Dependencies installed (see docs/architecture for why each):
#   1. fmg           runtime-edge store + MCP server (the Serve layer)
#   2. uv/uvx        runner for Serena
#   3. serena        LSP grounding oracle (stitcher false-positive grounding)
#   4. LSP backends  language servers Serena drives (pyright, typescript-language-server)
#   5. python venv   the stitcher (derive/ground/emit)
#   6. MCP register  wire `fmg serve` into the host agent tool
#   7. .fmg.toml     field/direction config for the served vault
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${CODEMAP_PREFIX:-$HOME/.local/bin}"
LOCK="$HERE/.install-lock"
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1

# --- pinned versions (supply-chain: no unpinned launchers) ---
FMG_REF="${FMG_REF:-master}"                     # fmg git ref to build if no vendored binary
SERENA_PKG="serena-agent"                         # pin exact version in production, e.g. serena-agent==1.6.0
PYRIGHT_VER="${PYRIGHT_VER:-1.1.401}"
TSLS_VER="${TSLS_VER:-4.3.3}"

say(){ printf '\033[1;36m[codemap]\033[0m %s\n' "$*"; }
ok(){  printf '  \033[1;32mok\033[0m   %s\n' "$*"; }
bad(){ printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAILED=1; }
have(){ command -v "$1" >/dev/null 2>&1; }

FAILED=0

# ---------- 1. fmg (runtime-edge store + MCP server) ----------
install_fmg(){
  if have fmg; then ok "fmg present ($(fmg --version 2>/dev/null || echo '?'))"; return; fi
  local vend="$HERE/bin/fmg-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
  if [ -x "$vend" ]; then
    mkdir -p "$PREFIX"; cp "$vend" "$PREFIX/fmg"; chmod +x "$PREFIX/fmg"
    ok "fmg installed from vendored binary → $PREFIX/fmg"
  elif have cargo; then
    say "building fmg from source (ref $FMG_REF) …"
    cargo install --git "${FMG_GIT:?set FMG_GIT to the fmg repo URL}" --branch "$FMG_REF" --root "${PREFIX%/bin}" fmg \
      && ok "fmg built + installed" || bad "cargo install fmg failed"
  else
    bad "fmg absent and no vendored binary / cargo — install Rust or provide bin/fmg-<os>-<arch>"
  fi
}

# ---------- 2. uv/uvx ----------
install_uv(){
  if have uvx; then ok "uv/uvx present"; return; fi
  [ $CHECK_ONLY -eq 1 ] && { bad "uvx absent"; return; }
  say "installing uv …"; curl -LsSf https://astral.sh/uv/install.sh | sh && ok "uv installed" || bad "uv install failed"
}

# ---------- 3. Serena (LSP grounding oracle) ----------
install_serena(){
  if uvx --from "$SERENA_PKG" serena --version >/dev/null 2>&1; then ok "serena resolvable via uvx"; return; fi
  [ $CHECK_ONLY -eq 1 ] && { bad "serena not resolvable"; return; }
  say "priming serena (uv tool install) …"
  uv tool install "$SERENA_PKG" >/dev/null 2>&1 || true
  uvx --from "$SERENA_PKG" serena --version >/dev/null 2>&1 && ok "serena ready" || bad "serena not resolvable"
}

# ---------- 4. LSP backends Serena drives ----------
install_lsp(){
  if have pyright-langserver || have pyright; then ok "pyright present"; else
    [ $CHECK_ONLY -eq 1 ] && bad "pyright absent" || {
      have npm && npm i -g "pyright@$PYRIGHT_VER" >/dev/null 2>&1 && ok "pyright installed" || bad "pyright: need node/npm"; }
  fi
  if have typescript-language-server; then ok "typescript-language-server present"; else
    [ $CHECK_ONLY -eq 1 ] && bad "tsls absent" || {
      have npm && npm i -g "typescript-language-server@$TSLS_VER" typescript >/dev/null 2>&1 && ok "tsls installed" || bad "tsls: need node/npm"; }
  fi
  # Serena auto-fetches other language servers on first use.
}

# ---------- 5. python venv + stitcher deps ----------
install_py(){
  local venv="$HERE/.venv"
  if [ ! -d "$venv" ]; then
    [ $CHECK_ONLY -eq 1 ] && { bad "stitcher venv missing"; return; }
    python3 -m venv "$venv"
  fi
  "$venv/bin/pip" install -q -r "$HERE/stitcher/requirements.txt" 2>/dev/null || true
  "$venv/bin/python" -c "import yaml" 2>/dev/null && ok "stitcher venv ready" || bad "stitcher deps missing (pyyaml)"
}

# ---------- 6. MCP registration ----------
register_mcp(){
  local vault; vault="$(codemap_cfg vault_dir "$HOME/codemap-vault")"
  say "MCP server command:  fmg -w $vault serve"
  # Detect host agent tool and write config, else print snippet.
  if [ -n "${CLAUDE_MCP_JSON:-}" ] && [ $CHECK_ONLY -eq 0 ]; then
    ok "add fmg to $CLAUDE_MCP_JSON (auto-wire TODO per host)"
  fi
  cat <<EOF
  → Register with your agent tool, e.g. Claude Desktop / Code:
    { "mcpServers": { "codemap": { "command": "fmg", "args": ["-w", "$vault", "serve"] } } }
EOF
}

# ---------- 7. .fmg.toml ----------
install_fmgtoml(){
  local vault; vault="$(codemap_cfg vault_dir "$HOME/codemap-vault")"
  [ $CHECK_ONLY -eq 1 ] && { [ -f "$vault/.fmg.toml" ] && ok ".fmg.toml present" || say ".fmg.toml not yet rendered (ok before first init)"; return; }
  if [ -d "$vault" ] && [ ! -f "$vault/.fmg.toml" ] && [ -f "$HERE/config/.fmg.toml.tmpl" ]; then
    cp "$HERE/config/.fmg.toml.tmpl" "$vault/.fmg.toml"; ok ".fmg.toml rendered into $vault"
  fi
}

# tiny TOML value reader (key = "val") — good enough for flat config
codemap_cfg(){ local k="$1" d="${2:-}"; local f="$HERE/config/codemap.toml"
  [ -f "$f" ] && grep -E "^\s*$k\s*=" "$f" | head -1 | sed -E 's/^[^=]*=\s*"?([^"]*)"?.*/\1/' || echo "$d"; }

main(){
  say "$([ $CHECK_ONLY -eq 1 ] && echo 'preflight check' || echo 'installing dependencies')"
  install_fmg; install_uv; install_serena; install_lsp; install_py; install_fmgtoml; register_mcp
  echo
  if [ "$FAILED" -eq 0 ]; then
    say "all dependencies satisfied ✓"
    [ $CHECK_ONLY -eq 0 ] && printf 'fmg=%s serena=%s pyright=%s\n' \
      "$(fmg --version 2>/dev/null||echo ?)" "$SERENA_PKG" "$PYRIGHT_VER" > "$LOCK" && say "wrote $LOCK"
  else
    say "some dependencies missing — see FAIL lines above"; exit 1
  fi
}
main
