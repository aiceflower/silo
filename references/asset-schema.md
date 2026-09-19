# Storage contract v1

The default root is the parent of `scripts/`; `--root` overrides it. The store creates `data/assets.db` and `data/config.json`. Relative attachment paths are rooted there. Never edit the database directly from the Agent.

SAVE accepts only: title, asset_type, source_url (HTTP/S without credentials), file_path (local source to copy), summary, core_value, content_text, content_html, user_note, metadata_json (object), tags (array), explore_level (none/basic). Title is normally a required nonempty string. A `capture` may omit it when it contains a file or nonempty `content_text`; the store derives the title from `metadata_json.filename`, the source filename, or the first text line. Type: github, website, article, image, capture, idea, note, html, pdf, bookmark, other. `capture` means unprocessed inbox text or a file, defaults to `explore_level: none`, and does not replace the `image` type used when the image itself is the intended asset. The default type is URL-inferred or idea. Files are hashed and copied. JSON fields must be objects, not JSON strings. `content_html` stores sanitized rich content; when supplied it produces the searchable plain-text projection in `content_text`. `user_note` is reserved for later personal annotations. Attachments default to a 20 MiB limit.

The local web form may create the same records through `POST /api/assets` for text/URL assets and `POST /api/assets/upload` for attachments. The new-asset page defaults to `capture`; either pasted text or a selected file is required, while title and instructions are optional. Capture tags may be selected from quick choices or entered as comma-separated text; both are explicit user tags, are deduplicated together, and are stored with `source=user`. These endpoints call this same store with `source=user`; they are not a separate database or import format.

Example:

```json
{"title":"网页抓取工具","asset_type":"website","source_url":"https://example.com/","summary":"用户提供的网页抓取工具。","core_value":"用于以后采集网页。","tags":[{"name":"网页抓取","facet":"capability","confidence":0.9}]}
```

A save result is `{asset,duplicate}`. Asset fields include id, asset_type, title, source_url, canonical_url, local_path, content_hash, summary, core_value, content_text, content_html, user_note, metadata_json, analysis_json, explore_level, status, revision, created_at, updated_at, last_explored_at, last_explore_attempt_at, last_explore_error, tags, and embeds. Each embed includes its ID, Skill-relative local path, MIME type, filename, byte size, and creation time so an Agent can inspect rich-text images without the web service. Timestamps are UTC ISO 8601.

Repeated URL or file saves return the existing ID; only empty derived fields are filled. User notes, titles, successful reports and existing content remain. An idea without URL/file is not forcibly deduplicated. URL and file collisions pointing to two different assets require manual resolution.

UPDATE accepts revision (required), title, summary, core_value, content_text, content_html, user_note, metadata_json, analysis_json, explore_level (none/basic/deep), status (active/archived), asset_type, source_url, canonical_url, tags. Sending `content_html` sanitizes active content, rejects remote images, preserves managed Silo images, and replaces `content_text` with the derived searchable text. Sending tags is an explicit replacement by the user; omitted fields are preserved. There is no permanent delete. On conflict the command fails; read again before reconciling.

The browser stores rich-text images in `asset_embeds`, with relative files under `storage/embeds/{asset_id}/`. Only PNG, JPEG, GIF, and WebP are accepted; file signatures are checked instead of trusting the browser MIME type. HTML may reference only the matching asset's managed image endpoint. Embedded images are included in backup, export, restore, package migration, and storage statistics.

SEARCH accepts --q, --status active/archived/all, --asset-type, --explore-level, repeated --tag TAG_ID, --page and --page-size. JSON result: items, total, page, page_size. Terms are combined with AND. Search includes the report and tag aliases. Chinese short terms use substring matching alongside FTS5. Results are not semantic embeddings.

IMPORT accepts --file and optional --preview. No per-link browsing. Preview does not insert records; actual import deduplicates again and merges folder sources. Limit 10,000 entries and 20 MiB by default (configurable in data/config.json).

REINDEX rebuilds the FTS index from saved assets atomically; it does not change source records.

BACKUP accepts --destination pointing to a new directory outside the current root. It remains the low-level directory snapshot command.

EXPORT writes a portable ZIP containing a manifest, consistent SQLite snapshot, config, and storage. RESTORE accepts that ZIP and the exact confirmation `恢复备份`; it validates paths, format, schema, database integrity, asset count, and referenced attachments before replacement. CLEAR accepts the exact confirmation `清空全部数据`, creates a fresh database, and removes all attachments without retaining an automatic backup. PACKAGE creates a runnable empty Silo Skill ZIP from code and a newly initialized database; it never copies the current database or attachments. Use PACKAGE, not EXPORT, when sharing the Skill with another person.

IMPORT now assigns bounded offline topic tags from title/URL/folders (source=import), retaining useful folder labels. Unknown resources are marked 待分类; these are provisional classifications, not researched facts.

`classify-bookmarks --preview` previews backfill for existing imported records without tags; `classify-bookmarks` applies it transactionally. Existing tagged records, reports, status and update ordering are preserved; revision increments to protect concurrent edits. Back up before bulk backfill.
