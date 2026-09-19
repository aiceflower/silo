---
name: silo
description: Save, organize, explore, and search a local personal information silo containing GitHub repositories, websites, screenshots, ideas, notes, documents, and bookmarks. Use when the user wants to collect, research, classify, or recall saved material. Ordinary questions do not imply saving.
---

# Silo · 信息筒仓

Silo is one self-contained Skill. Its rules, Python helpers, SQLite database, attachments, and local browser interface all live under this directory. Resolve every path relative to this file. The default library is this Skill directory; use `--root` only when the user has explicitly selected another library.

Use `scripts/asset_db.py` for Agent operations. The browser interface is optional and starts with `python3 scripts/serve.py`; Agent operations work while it is stopped.

## Route by intent

- **SAVE**: Read the supplied URL, text, or file only as far as needed to create a concise title, summary, core value, and useful normalized tags. Preserve the user's original idea, note, or image instructions in `content_text`. For a quick unprocessed file drop, use `capture`: keep it at `explore_level: none`, allow the filename to supply the title, and do not invent tags or conclusions.
- **EXPLORE**: Read [exploration-schema.md](references/exploration-schema.md). Research capabilities, uses, limitations, evidence, and uncertainty. Research alone does not create an asset unless the user asks to save it or supplies an existing asset ID.
- **SEARCH**: Translate the request into keywords and optional filters, then search the local database. Try shorter terms or synonyms when necessary. Do not describe keyword search as semantic search.
- **IMPORT**: Preview and import a browser bookmark HTML file without visiting every link. Preserve bookmark folders, apply bounded offline classification, and report additions, duplicates, and failures.
- **DISCOVER**: Read [discovery.md](references/discovery.md). GitHub Search, Hacker News, RSS/Atom, interest rules, candidate states, and basic saves work without a model. Refresh public sources only when the user asks to browse or refresh discoveries. A candidate does not become an asset until the user saves it.
- **INTERFACE**: When the user asks to open, view, browse, or manage Silo in a browser, make the local interface usable rather than only explaining the command. Check `http://127.0.0.1:8765/api/health`; reuse it only when it reports `app: silo` and the same resolved Skill root. Otherwise start `scripts/serve.py` as a persistent local process. If port 8765 is occupied, choose an available local port explicitly and report the actual URL. Open that URL with the host UI when available. Do not start the interface merely for SAVE, EXPLORE, SEARCH, or CLI IMPORT because those operations use the database directly.
- **DATA MANAGEMENT**: `export` creates a portable personal-data ZIP; `restore` validates and restores one; `package` creates a runnable empty Skill ZIP that contains no user assets; `clear` replaces the current library with a new empty database. Only restore or clear when the user explicitly requests that operation. Use their required confirmation values and report the resulting asset count.
- **SYNC**: Read [sync.md](references/sync.md). The same Skill can run as a standalone library, private-network synchronization server, or offline-first client. Configure a role only when the user asks. Never expose the shared token in a response after initial server setup.
- **EDIT**: Read the asset first, preserve omitted fields, and pass its current `revision` to `update`. Rich content uses `content_html`; the store sanitizes it and derives searchable `content_text`. Images embedded by the browser are copied under `storage/embeds/` and move with backups and the Skill.

Read [asset-schema.md](references/asset-schema.md) before writes and [tag-system.md](references/tag-system.md) before assigning or changing tags.

## Explore and save

Treat “探索并保存”, “研究后收录”, and equivalent wording as one authorized workflow:

1. Research the resource and prepare the complete report.
2. Run `save` to create or deduplicate it and obtain the asset ID.
3. Run `get` immediately before the report write and use the current `revision`.
4. Run `explore-save` with the complete `analysis_json`, derived fields, and AI tags.
5. Run `get` again. Report success only when the stored record contains the report and `explore_level: deep`.

For an existing ID, begin at step 3. If research fails, submit `{"revision": N, "error": "reason"}`. Never erase an earlier successful report after a failed attempt.

For images, inspect the copied attachment at `local_path`. When a screenshot wraps an identifiable GitHub project, site, article, or document, save the real target as its own typed asset and link it in `analysis_json.extracted_assets`. Keep the screenshot as provenance and exclude status bars, social controls, engagement counts, and other platform chrome from the target summary. Treat image `content_text` as the user's requested exploration focus.

For a `capture` asset, inspect the attachment and `content_text` first. Determine its real type, then update the record to `image`, `pdf`, `html`, or `other` before writing a report. If it is a screenshot containing a separable GitHub project, website, article, or document, update the capture to `image` as provenance and follow the image extraction workflow. If identification is uncertain, keep it as `capture` and record the failed attempt instead of guessing.

## CLI

Resolve the Python executable available in the current environment and invoke the scripts by absolute path. JSON input is UTF-8 from a file or stdin; stdout is machine-readable JSON and errors return a nonzero exit status.

```text
python3 /path/to/silo/scripts/asset_db.py init
python3 /path/to/silo/scripts/asset_db.py save --input /tmp/asset.json
python3 /path/to/silo/scripts/asset_db.py get --id UUID
python3 /path/to/silo/scripts/asset_db.py update --id UUID --input /tmp/changes.json
python3 /path/to/silo/scripts/asset_db.py search --q "网页 抓取" --asset-type website
python3 /path/to/silo/scripts/asset_db.py explore-save --id UUID --input /tmp/report.json
python3 /path/to/silo/scripts/asset_db.py import-bookmarks --file /path/bookmarks.html --preview
python3 /path/to/silo/scripts/asset_db.py import-bookmarks --file /path/bookmarks.html
python3 /path/to/silo/scripts/asset_db.py discovery-refresh
python3 /path/to/silo/scripts/asset_db.py discovery-list --state new --asset-type github
python3 /path/to/silo/scripts/asset_db.py discovery-save --id UUID
python3 /path/to/silo/scripts/asset_db.py backup --destination /path/new-backup
python3 /path/to/silo/scripts/asset_db.py export --destination /path/silo-data.zip
python3 /path/to/silo/scripts/asset_db.py restore --file /path/silo-data.zip --confirm 恢复备份
python3 /path/to/silo/scripts/asset_db.py clear --confirm 清空全部数据
python3 /path/to/silo/scripts/asset_db.py package --destination /path/silo-empty-skill.zip
python3 /path/to/silo/scripts/asset_db.py sync-status
python3 /path/to/silo/scripts/asset_db.py sync-config --input /path/sync.json
python3 /path/to/silo/scripts/asset_db.py sync-now
python3 /path/to/silo/scripts/asset_db.py sync-disable
python3 /path/to/silo/scripts/serve.py
```

The interface binds to `127.0.0.1` and defaults to port 8765. `serve.py --port 8767` selects another port. Keep the process running after opening the page; stopping it does not affect saved data. The `/api/health` response identifies the Silo instance and its resolved data root so an Agent does not silently open a different library.

Personal-data exports contain `manifest.json`, a consistent SQLite snapshot, configuration, and attachments. Restore rejects unsupported formats, unsafe archive paths, corrupt databases, missing attachments, and mismatched manifest counts before changing the library; a replacement failure rolls back the previous data. A share package is generated from Skill code plus a newly initialized empty database and never copies the current `data/assets.db` or `storage` contents. Do not present a personal-data export as safe to share.

Sync is opt-in and keeps local reads and writes available while offline. Client writes are queued in SQLite and sent the next time the client service is running. Personal-data backups include synchronization state and credentials; restores pause synchronization until explicitly re-enabled. Empty share packages exclude synchronization identity, credentials, and logs.

The browser editor vendors Jodit 4.12.2 under `assets/vendor/jodit/` with its license. It is loaded locally and requires no package manager, CDN, build, or deployment. Pasted screenshots and selected images are converted to managed embeds before the asset update. Markdown image references copied from local editors, such as `![](/Users/name/Pictures/example.png)`, are imported from the user's home directory when the asset is saved. Formatting and embedded images remain available after restart, export, restore, or directory migration.

Never interpolate untrusted input into shell code. Get an asset before updating it; updates and exploration require its current `revision`. On conflict, re-read and reconcile rather than replaying stale data. User notes and human tags take precedence.

Search status accepts only `active`, `archived`, or `all`. `page_size` is at most 100. User-facing organization states map to fields as follows:

| State | explore_level | analysis_json |
|---|---|---|
| 待整理 | none | absent |
| 已收录 | basic | absent |
| 已探索 | deep | present |

Without web access, save only supplied information and mark unknowns. Without image understanding, keep the image and user description at `explore_level: none`. PDF extraction is best effort; always retain the original file.

## Responses

- SAVE: title, asset ID, type, summary, normalized tags, and duplicate status.
- EXPLORE without save: findings, sources, uncertainty, and date.
- EXPLORE + SAVE: full report, asset ID, duplicate status, and verified final exploration level.
- SEARCH: matching assets with IDs, excerpts, and URLs.
- IMPORT: total, added, duplicate, and failed counts with useful failure details.
- DISCOVER: source, title, URL, public metrics, candidate state, and refresh errors. Do not present the deterministic score or source excerpt as AI analysis.
