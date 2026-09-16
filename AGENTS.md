# Silo development instructions

## Project context

Silo is a self-contained, portable personal information asset Skill. This directory contains its Python code, SQLite database, native browser interface, attachments, tests, and Agent instructions.

- The library is local-first and must remain fully usable offline.
- Synchronization is opt-in and may be enabled only when the user explicitly requests it.
- The browser service is optional. CLI and Agent operations must work while it is stopped.
- Read `SKILL.md` for user-facing SAVE, EXPLORE, SEARCH, IMPORT, DISCOVER, editing, data-management, and synchronization workflows. Do not duplicate those workflows here.

## Architecture and sources of truth

- `SKILL.md`: user-facing Agent behavior and workflow routing.
- `references/`: maintained contracts for assets, exploration, tags, discovery, and synchronization.
- `scripts/store.py`: the storage core and database migration entry point.
- `scripts/asset_db.py`: the JSON CLI used by Agents and local automation.
- `scripts/serve.py`: the optional local browser and HTTP service.
- `scripts/sync.py`: private-network client synchronization.
- `assets/ui/`: native HTML, CSS, and JavaScript with no build step.
- `data/` and `storage/`: real runtime data, never test fixtures.
- `tests/`: tests that must use isolated temporary Silo roots.

Resolve paths relative to this directory. Follow the relevant reference document before changing its contract.

## Data and architecture invariants

- Never directly edit, delete, replace, or rebuild the live `data/assets.db`.
- Never let tests read from or write to the live `data/` or `storage/` directories.
- Route business-data mutations through `Store` so validation, revisions, search indexes, and synchronization logs remain consistent.
- Before a database schema change, create a consistent backup, add an explicit forward migration, and preserve upgrades from supported older schemas.
- A database migration must never be implemented by deleting and recreating the user's database.
- Store attachment paths relative to the Skill root and preserve traversal and containment checks.
- Keep the interface in native HTML, CSS, and JavaScript. Do not add a Node build chain, frontend framework, or runtime CDN dependency.
- When vendoring a browser dependency, keep its license beside the vendored files.
- Keep the default mode `standalone`. Do not enable server/client mode, listen on the LAN, or reveal a sync token without an explicit user request.
- Keep ordinary pages and asset APIs loopback-only. LAN access is limited to authenticated synchronization endpoints.
- Preserve the privacy boundaries of backup, restore, clear, and empty share-package operations.

## Documentation synchronization

- After every code change, check `SKILL.md` and the relevant files under `references/` for accuracy.
- Update documentation in the same change whenever user-visible behavior, CLI commands, HTTP APIs, data structures, workflows, security constraints, or defaults change.
- Put detailed data contracts in the matching reference. Put only rules needed across ordinary Skill use in `SKILL.md`.
- Pure internal refactors and behavior-neutral test changes do not require artificial edits to `SKILL.md`.
- Keep this file focused on development constraints. Do not copy full command catalogs, JSON schemas, exploration templates, or synchronization protocol details from their authoritative documents.

## Change workflow

1. Read the relevant code, `SKILL.md`, and applicable reference before editing.
2. Confirm that live data has a consistent backup before changing storage, migrations, restore behavior, or synchronization persistence.
3. Exercise data operations only against a temporary Silo root.
4. Run focused checks while iterating, then run the required validation before handoff.
5. Restart the local service after changing Python server code. Open and inspect the affected page after interface changes.
6. For database changes, verify integrity, foreign keys, schema version, and preservation of the asset count.
7. Report the behavior changed, tests run, migration impact, and any material limitation.

## Required validation

Run from this directory:

```bash
python3 -m py_compile scripts/*.py
node --check assets/ui/app.js
python3 -m pytest -q tests
```

After changing `SKILL.md` or a Skill reference, also run the Skill validator when it is available:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" .
```

Do not run a formatter or generator that rewrites unrelated files. Do not claim completion when a required check fails; either fix the failure or report the exact blocker.
