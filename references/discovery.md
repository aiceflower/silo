# Discovery without a model

Discovery is an optional local reading inbox. It fetches public structured metadata and never calls a language model.

## Sources

- `github`: GitHub repository search for recently created, non-archived repositories. Metadata includes description, stars, forks, language, topics, license and timestamps. `GITHUB_TOKEN` is optional and is read only from the process environment.
- `hackernews`: official Top Stories and item endpoints. The original article URL and discussion metrics are retained.
- `rss` / `atom`: user-added HTTP(S) feeds parsed as XML. Title, link, source excerpt, author, date and categories are retained. Local and private-network feed addresses are rejected.

The page paints cached SQLite results immediately. When any source is older than `discovery_cache_minutes` (30 minutes by default), it starts a background network refresh and repaints after completion. “刷新全部” and each source refresh button always force a live request. Responses are size-limited, time-limited and TLS-verified. One failing source does not discard previously fetched candidates.

## Candidate states

`new` is the default inbox. `interested` means the user wants to revisit it. `dismissed` hides it from the inbox. `saved` links it to a real Silo asset.

Discovery candidates stay separate from `assets`. Saving a candidate creates or deduplicates a `github` or `article` asset at `explore_level: basic`, preserving source metrics and topics in metadata. Saving does not create a deep report. The user may later copy the asset exploration instruction and ask an Agent to research it.

## Scoring and filters

GitHub items receive a transparent score from stars, forks and repository age. Hacker News uses points and discussion count. Enabled interest rules add or subtract fixed weights for matching include words, exclude words, languages, topics and minimum stars. This score only orders the inbox and is not an AI recommendation.

The browser interface can filter candidates by type, keyword, GitHub language and topic. RSS categories appear as topics when supplied by the feed.
