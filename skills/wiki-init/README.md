# skills/wiki-init/

**Cold-start skill**: turn a codebase with no wiki into a served navigation map with one page per
real subsystem.

`SKILL.md` is the skill itself (the orchestration an agent follows: survey -> page inventory ->
bounded per-page workers -> verify loop). `wiki-init.sh` is the orchestration helper it calls,
whose presence the packaging contract requires (criterion 7); the page-inventory backbone it drives
lives in `../../init/inventory.py`.

The design intent is that coverage is decided explicitly and up front rather than emerging from a
long generation run — see `../../docs/cold-start-contract.md` for the interface both this skill and
`tests/test_inventory.py` are held to.
