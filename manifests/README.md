# Host manifests — wiring codemap into any agent tool

`codemap` is tool-agnostic. Its primary manifest is the **Claude Code plugin** at
`../.claude-plugin/plugin.json` (with `../.mcp.json`), which Claude Code auto-discovers when the
package is loaded as a plugin (`claude --plugin-dir <path-to-codemap>`). This directory holds
**shims** for other hosts that use a different config shape but the same underlying command:

    fmg -w <vault> serve        # the MCP server every host launches

`install.sh` detects the host and wires the right file; these templates are the manual fallback.
Replace `<CODEMAP_ROOT>` with the absolute path to this package and `<VAULT>` with your served
vault directory (the prose-wiki dir from `config/codemap.toml`'s `vault_dir`).

| Host | Config file | Shim |
|------|-------------|------|
| Claude Code | `.claude-plugin/plugin.json` + `.mcp.json` (this package) | native — load with `--plugin-dir` |
| opencode | `opencode.json` (`mcp` block) | `opencode.json` |
| Cursor / generic MCP | `.cursor/mcp.json` (or the host's `mcpServers`) | `cursor.mcp.json` |

Skills (`../skills/`) are SKILL.md dirs. Claude Code auto-discovers them from the plugin; for
opencode, point the agent config at `../skills/`; for hosts without a skill system, the SKILL.md
files are still readable prose runbooks.
