# Faceted tags

Facets: domain (领域), capability (能力), scenario (场景), technology (技术).

Call `asset_db.py tags --q TERM` before inventing tags. Prefer a matching canonical label or true synonym in the same facet. Normalization uses NFKC, case folding and whitespace normalization. Related concepts are not synonyms: 知识库 and 知识管理 remain distinct. Do not collapse facets.

Tag input: `{name,facet,confidence}`. AI confidence must be 0..1; values below 0.65 are dropped. Prefer 3–8 useful tags but never fill a quota. Human tags are protected from AI replacement; a user edit may explicitly replace the full list.

To register a verified alias, run `asset_db.py alias --input JSON_FILE` with `{tag_id,alias}`. Same-facet conflicts are rejected. Do not create aliases from superficial similarity. `tags` returns usage_count and aliases for inspection.
