import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from discovery import feed_items, rule_score  # noqa: E402
from store import Error  # noqa: E402


def test_discovery_candidate_lifecycle_and_asset_save(store):
    defaults = {x["name"] for x in store.discovery_sources()}
    assert {"OpenAI News", "Google DeepMind", "Hugging Face Blog", "GitHub Engineering"} <= defaults
    source = next(x for x in store.discovery_sources() if x["source_type"] == "github")
    item = {
        "external_id": "repo-1", "item_type": "github", "title": "example/tool",
        "url": "https://github.com/example/tool", "summary": "A deterministic tool",
        "author": "example", "published_at": "2026-09-01T00:00:00+00:00",
        "metrics_json": {"stars": 420, "forks": 20, "language": "Python"},
        "topics_json": ["automation", "cli"], "score": 88,
    }
    assert store.ingest_discovery(source["id"], [item]) == {"added": 1, "updated": 0, "total": 1}
    assert store.ingest_discovery(source["id"], [{**item, "summary": "Updated"}])["updated"] == 1
    candidate = store.discovery_items(item_type="github")["items"][0]
    assert candidate["summary"] == "Updated" and candidate["metrics_json"]["stars"] == 420
    assert store.set_discovery_state(candidate["id"], "interested")["state"] == "interested"
    saved = store.save_discovery_item(candidate["id"])
    assert saved["asset"]["asset_type"] == "github" and saved["asset"]["explore_level"] == "basic"
    assert saved["discovery_item"]["state"] == "saved"
    assert store.save_discovery_item(candidate["id"])["duplicate"] is True
    assert store.search("Updated")["items"][0]["id"] == saved["asset"]["id"]


def test_rss_and_atom_parsing_without_model():
    rss = b'''<rss><channel><item><guid>one</guid><title>Python &amp; SQLite</title><link>https://example.com/one</link><description><![CDATA[<b>Useful</b> article]]></description><pubDate>Sat, 12 Sep 2026 10:00:00 GMT</pubDate><category>Python</category></item></channel></rss>'''
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>two</id><title>Agent Tools</title><link href="/two"/><summary>Build tools</summary><updated>2026-09-12T10:00:00Z</updated></entry></feed>'''
    first = feed_items(rss, "https://example.com/feed.xml")[0]
    second = feed_items(atom, "https://example.com/feed.xml")[0]
    assert first["title"] == "Python & SQLite" and first["summary"] == "Useful article"
    assert first["topics_json"] == ["Python"] and second["url"] == "https://example.com/two"


def test_interest_rules_are_explicit_and_affect_score(store):
    rule = store.save_interest_rule({"name": "Python tools", "include_keywords": ["automation"], "exclude_keywords": ["course"], "languages": ["Python"], "topics": [], "min_stars": 100})
    item = {"title": "Automation CLI", "summary": "", "author": "", "topics_json": [], "metrics_json": {"language": "Python", "stars": 200}}
    assert rule_score(item, [rule]) == 43
    assert rule_score({**item, "title": "Automation course"}, [rule]) < 0
    with pytest.raises(Error):
        store.save_interest_rule({"name": "broken", "include_keywords": "not-a-list"})
