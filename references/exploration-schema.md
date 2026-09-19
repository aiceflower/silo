# Exploration contract v1

Read the saved asset and its revision first. Successful input to explore-save is `{revision, analysis_json, summary?, core_value?, tags?}`. Failure input is `{revision,error}`; it retains the prior successful report. Source facts must be verified from actual retrieved material, with dates and uncertainty. Do not treat unknown as false or free.

All successful reports include:

```json
{
  "schema_version":1,
  "asset_type":"website",
  "generated_at":"2026-09-11T00:00:00+00:00",
  "sources":[{"url":"https://example.com/","title":"Source title","accessed_at":"2026-09-11T00:00:00+00:00"}],
  "unknowns":["未实际验证使用效果"],
  "report_markdown":"# 网站探索\n\n在这里保留完整分析、证据和推断。",
  "positioning":"项目定位",
  "core_features":[],
  "use_cases":[],
  "strengths":[],
  "limitations":[],
  "alternatives":[],
  "personal_value":"为什么对用户有价值",
  "recommendation":"try"
}
```

The sample URL and time are illustrative; replace them with observed sources and actual time. Recommendation: use_now / try / keep / watch / archive. It never automatically archives the asset. No sources available: explicitly state it and unknowns; do not invent citations.

GitHub additionally requires problems_solved[], tech_stack[], usage_modes[], learning_cost (low/medium/high/unknown), maturity {level,reason}. Cover purpose, audience, actual capabilities, onboarding, releases/maintenance evidence, limitations, alternatives, personal relevance. A Star count alone is not maturity evidence.

Websites additionally describe login_required (true/false/null) and usage_cost (string or null); state unknown if not verified.

For images, inspect the saved attachment when image understanding is available. Keep the common envelope and add `visual_summary`, `extracted_text`, `key_elements`, `possible_uses`, and `follow_up_questions`. Distinguish visible text from interpretation. Never infer obscured or unreadable details. If image understanding is unavailable, preserve the asset at `none` and record the failed attempt instead of fabricating a report.

For an unprocessed `capture`, inspect the attachment and user instructions before producing a report. Classify it by updating the same record to `image`, `pdf`, `html`, or `other`, then use that type's exploration rules. A screenshot that wraps another identifiable resource becomes an `image` provenance record and follows the extraction workflow below. Do not store a successful deep report with `asset_type: capture`; when the material cannot be identified reliably, retain the capture at `none` and record the failed attempt.

### Images that wrap another resource

When an image is a social post, chat screenshot, slide, or browser capture and the user's real target is a GitHub repository, website, article, book, or tool mentioned inside it, treat the image as source evidence rather than the final knowledge asset:

1. Read `content_text` to determine what the user wants extracted.
2. Ignore interface chrome and incidental metadata unless the user asks for it: status bars, avatars, author controls, reactions, comments, share buttons, navigation and decorative mockups are normally noise.
3. Identify candidate names and URLs from visible content, then verify each target from its primary source. Do not create a target when identity remains ambiguous.
4. For every verified target, run the normal combined explore-and-save workflow using the target's true asset type. URL deduplication must reuse an existing asset.
5. Keep the image as provenance. Its report should briefly record the relevant excerpt, what was ignored and why, and an `extracted_assets` array containing `{asset_id, asset_type, title, source_url, relationship}` for each created or reused target. Use `relationship: "extracted_from"`.
6. Put detailed project or website analysis on the extracted target. Do not duplicate the full target report on the image record. Never delete the original image automatically.

If the image contains only visual inspiration and no separable external resource, keep it as an image asset and analyze the visual content normally. If it contains several useful targets, create several extracted assets rather than merging them into one report.

For ideas, keep the user's original text intact and add `problem`, `proposal`, `target_users`, `assumptions`, `risks`, `next_steps`, and `related_assets` where evidence exists. Exploration develops the idea; it must label new suggestions as analysis rather than rewriting them as the user's words.

For notes, preserve the source note and add `key_points`, `decisions`, `open_questions`, `related_topics`, `next_actions`, and `related_assets`. A note exploration may organize and connect what was written, but must not invent decisions or commitments.

The markdown is the complete detailed report; structured fields are its consistent summary, submitted atomically. The interface renders Markdown without raw HTML. Historical report versions are not stored in v1.
