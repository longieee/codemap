# .claude-plugin/

The Claude Code **plugin manifest** for this pack. `plugin.json` is what makes the directory
loadable by `claude --plugin-dir <path-to-codemap>`: it declares the plugin's name, version and
description, and points at the sibling `../.mcp.json`, which registers exactly one MCP server
(`fmg serve`, resolved through `${CLAUDE_PLUGIN_ROOT}/bin/fmg`).

Contract: `docs/packaging-contract.md` criteria 1-4 (valid JSON, `name == "codemap"`, semver
`version`, non-empty `description`, exactly one MCP server whose command is plugin-root-relative
and whose args include `serve`). Other agent tools are served by the shims in `manifests/`.

The manifest carries no vendor or personal identity (criterion 22): `author` is generic and the
upstream URLs are placeholders. The real `fmg` source repo, if a machine has to build it from
source, is instance config — `fmg_git` in `config/codemap.toml`, or `$FMG_GIT`.
