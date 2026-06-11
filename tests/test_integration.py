"""
FROZEN FITNESS DEFINITION — sibling integration (news-exchange contract).

The evolution engine must NEVER modify this file. It pins the cross-app contract
(see contract/news_exchange.md): World Digest publishes public/digest.json in the
agreed schema, borrows per-country sentiment from the sibling, and — the cardinal
invariant — the digest must still ship when the sibling is unreachable.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import core  # noqa: E402


def _fake(title, region="Europe", source="Test", summary="headline body", link="https://x"):
    return {"title": title, "summary": summary, "link": link, "region": region, "source": source}


FAKE_ITEMS = [
    _fake("Earthquake strikes northern Japan, tsunami warning issued", region="Asia"),
    _fake("Tsunami warning after powerful earthquake strikes northern Japan", region="Asia"),
    _fake("US Federal Reserve raises interest rates amid inflation concerns", region="Americas"),
]


def _prep(monkeypatch, tmp_path, sentiment):
    """Make run() hermetic: no network, no real LLM, writes redirected to tmp."""
    monkeypatch.delenv("HF_TOKEN", raising=False)  # force extractive fallback
    monkeypatch.setattr(core, "fetch_feeds", lambda config: list(FAKE_ITEMS))
    monkeypatch.setattr(core, "send_email", lambda body: False)
    monkeypatch.setattr(core, "fetch_sibling_sentiment", lambda: sentiment)
    monkeypatch.setattr(core, "METRICS_PATH", tmp_path / "metrics.json")
    monkeypatch.setattr(core, "DIGEST_JSON_PATH", tmp_path / "public" / "digest.json")


def test_country_aliases_floor():
    """The shared contract vocabulary must stay broad and actually match."""
    assert len(core.COUNTRY_ALIASES) >= 40
    assert "Japan" in core._match_countries("A quake hit Tokyo today")
    assert "China" in core._match_countries("Officials in Beijing responded")


def test_emits_digest_json_contract(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, {"Japan": -0.5, "United States": 0.1})
    core.run()
    data = json.loads((tmp_path / "public" / "digest.json").read_text())
    assert data["schema"] == "world-digest/news-exchange@1"
    for key in ("generated_at", "digest_words", "clusters", "narrative"):
        assert key in data
    assert isinstance(data["clusters"], list) and data["clusters"]
    first = data["clusters"][0]
    for key in ("headline", "countries", "regions", "outlets", "summary", "links"):
        assert key in first


def test_digest_survives_sibling_outage(monkeypatch, tmp_path):
    """Sibling unreachable (empty sentiment) → digest still ships and publishes."""
    _prep(monkeypatch, tmp_path, {})
    metrics = core.run()
    assert metrics["sibling_reachable"] is False
    assert metrics["sentiment_enriched"] is False
    assert metrics["digest_words"] > 0
    assert (tmp_path / "public" / "digest.json").exists()


def test_fetch_sibling_sentiment_swallows_errors(monkeypatch):
    """A thrown request must degrade to {} — never propagate to the pipeline."""
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(core.requests, "get", boom)
    assert core.fetch_sibling_sentiment() == {}
