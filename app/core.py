"""
World Digest - core pipeline.
Fetch RSS feeds -> deduplicate stories -> summarize via LLM -> email digest.

This file is the EVOLVABLE part of the system. The mutation engine may
rewrite it, but it must always pass tests/test_core.py (frozen).
"""

import json
import os
import re
import smtplib
import socket
import ssl
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests

# A dead feed that accepts the connection but never responds would otherwise
# hang feedparser.parse() forever (no exception, so the try/except can't catch
# it) and stall the whole pipeline. Cap every socket wait so a stalled feed
# raises, hits the warn-and-continue path, and the digest still goes out.
socket.setdefaulttimeout(15)

CONFIG_PATH = Path(__file__).parent / "config.json"

# Summarization runs on Hugging Face's OpenAI-compatible Inference Providers
# router (free tier). Auth is an `hf_...` token with "Inference Providers"
# permission, passed via the HF_TOKEN env var. The ":cheapest" suffix tells the
# router to pick the lowest-cost provider serving the model; swap the model name
# to change quality/cost (e.g. meta-llama/Llama-3.1-8B-Instruct for a smaller one).
LLM_ENDPOINT = "https://router.huggingface.co/v1/chat/completions"
LLM_MODEL = "meta-llama/Llama-3.3-70B-Instruct:cheapest"

# --- Sibling integration (see contract/news_exchange.md) -------------------
# World Digest borrows per-country sentiment from the InsightsEngine globe over
# HTTP (best-effort) and publishes its own clustered digest to public/digest.json
# for the sibling to consume. The schema id is the frozen interface version; the
# country alias map and these paths live outside the evolvable file so a mutation
# can't drift the contract.
EXCHANGE_SCHEMA = "world-digest/news-exchange@1"
REPO_ROOT = Path(__file__).parent.parent
CONTRACT_PATH = REPO_ROOT / "contract" / "country_aliases.json"
METRICS_PATH = REPO_ROOT / "evolution" / "metrics.json"
DIGEST_JSON_PATH = REPO_ROOT / "public" / "digest.json"
SIBLING_GLOBE_URL = os.environ.get(
    "SIBLING_GLOBE_URL",
    "https://colesr-insight-engine-docker.hf.space/api/news/globe",
)
COUNTRY_ALIASES = json.loads(CONTRACT_PATH.read_text()) if CONTRACT_PATH.exists() else {}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "feeds": {
        "Europe": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://rss.dw.com/rdf/rss-en-world",
        ],
        "Middle East": [
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://www.middleeasteye.net/rss",
        ],
        "Asia": [
            "https://www3.nhk.or.jp/nhkworld/en/news/feeds/",
            "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
        ],
        "Americas": [
            "https://feeds.npr.org/1004/rss.xml",
            "https://rss.cbc.ca/lineup/world.xml",
        ],
        "Africa": [
            "https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf",
            "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
        ],
    },
    "max_items_per_feed": 15,
    "max_clusters_in_digest": 12,
    "digest_word_limit": 800,
    "similarity_threshold": 0.35,  # Increased from 0.25 to improve deduplication
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_feeds(config: dict) -> list[dict]:
    """Fetch all feeds. Returns list of {title, summary, link, region, source}."""
    items = []
    for region, urls in config["feeds"].items():
        for url in urls:
            try:
                parsed = feedparser.parse(url)
                if parsed.bozo and parsed.bozo_exception:
                    print(f"[warn] feed parsing issue: {url} ({parsed.bozo_exception})")
                for entry in parsed.entries[: config["max_items_per_feed"]]:
                    items.append(
                        {
                            "title": entry.get("title", "").strip(),
                            "summary": re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:500],
                            "link": entry.get("link", ""),
                            "region": region,
                            "source": parsed.feed.get("title", url),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - keep pipeline alive
                print(f"[warn] feed failed: {url} ({exc})")
                if "timed out" in str(exc):
                    print(f"[info] considering alternative feed sources for region: {region}")
                    # Placeholder for alternative feed logic
    return [i for i in items if i["title"]]


# ---------------------------------------------------------------------------
# Deduplicate / cluster (simple token-overlap similarity, no heavy deps)
# ---------------------------------------------------------------------------

STOPWORDS = set(
    "the a an and or of to in on for with as at by from is are was were be has have it its".split()
)


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z']+", text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _combined_similarity(item1: dict, item2: dict, title_weight: float = 0.7) -> float:
    """Calculate combined similarity based on title and summary with weighted average."""
    title_sim = _similarity(_tokens(item1["title"]), _tokens(item2["title"]))
    summary_sim = _similarity(_tokens(item1["summary"]), _tokens(item2["summary"]))
    return title_weight * title_sim + (1 - title_weight) * summary_sim


def cluster_items(items: list[dict], threshold: float) -> list[list[dict]]:
    """Improved clustering by combining title and summary similarity with iterative refinement."""
    clusters: list[dict] = []  # each: {"title_tokens": set, "summary_tokens": set, "items": [..]}
    
    # Sort items by title length (longer titles first) for better centroid representation
    sorted_items = sorted(items, key=lambda x: len(x["title"]), reverse=True)
    
    for item in sorted_items:
        item_title_tokens = _tokens(item["title"])
        item_summary_tokens = _tokens(item["summary"])
        best, best_score = None, 0.0
        
        # Find the best matching cluster
        for cluster in clusters:
            # Calculate combined similarity using both title and summary
            score = _combined_similarity(item, cluster["items"][0])
            if score > best_score:
                best, best_score = cluster, score
                
        # If we found a good match, add to that cluster
        if best is not None and best_score >= threshold:
            best["items"].append(item)
            # Update the cluster's token sets to improve future matching
            best["title_tokens"] |= item_title_tokens
            best["summary_tokens"] |= item_summary_tokens
        else:
            # Create a new cluster
            clusters.append({
                "title_tokens": set(item_title_tokens),
                "summary_tokens": set(item_summary_tokens),
                "items": [item]
            })
            
    # Second pass: Try to merge similar clusters to further reduce duplicates
    merged_clusters = []
    used_indices = set()
    
    for i, cluster_a in enumerate(clusters):
        if i in used_indices:
            continue
            
        # Start with the current cluster
        merged_cluster = {
            "title_tokens": set(cluster_a["title_tokens"]),
            "summary_tokens": set(cluster_a["summary_tokens"]),
            "items": list(cluster_a["items"])
        }
        used_indices.add(i)
        
        # Check for merge candidates
        for j, cluster_b in enumerate(clusters[i+1:], i+1):
            if j in used_indices:
                continue
                
            # Use a slightly lower threshold for cluster merging
            title_score = _similarity(cluster_a["title_tokens"], cluster_b["title_tokens"])
            summary_score = _similarity(cluster_a["summary_tokens"], cluster_b["summary_tokens"])
            combined_score = 0.7 * title_score + 0.3 * summary_score
            
            if combined_score >= threshold * 0.8:  # Slightly lower threshold for cluster merging
                merged_cluster["title_tokens"] |= cluster_b["title_tokens"]
                merged_cluster["summary_tokens"] |= cluster_b["summary_tokens"]
                merged_cluster["items"].extend(cluster_b["items"])
                used_indices.add(j)
                
        merged_clusters.append(merged_cluster)
    
    # Sort by cluster size (bigger clusters first = more widely covered = more important)
    merged_clusters.sort(key=lambda c: len(c["items"]), reverse=True)
    return [c["items"] for c in merged_clusters]


# ---------------------------------------------------------------------------
# Sibling enrichment (country tagging + borrowed sentiment)
# ---------------------------------------------------------------------------

def _match_countries(text: str) -> list[str]:
    """Tag text with canonical country names from the shared contract.
    Multi-word/punctuated aliases match as substrings; single words use \\b
    word boundaries (so "us" doesn't fire inside "bus")."""
    hay = text.lower()
    hits: list[str] = []
    for country, aliases in COUNTRY_ALIASES.items():
        for alias in aliases:
            if " " in alias or "." in alias:
                if alias in hay:
                    hits.append(country)
                    break
            elif re.search(r"\b" + re.escape(alias) + r"\b", hay):
                hits.append(country)
                break
    return hits


def _cluster_countries(cluster: list[dict]) -> list[str]:
    found: list[str] = []
    for item in cluster:
        for country in _match_countries(item["title"]):
            if country not in found:
                found.append(country)
    return found


def fetch_sibling_sentiment() -> dict:
    """Borrow per-country sentiment (-1..1) from the InsightsEngine globe.
    Best-effort: returns {} on any failure so a sibling outage can never break
    the digest (the coupling is additive — see contract/news_exchange.md)."""
    try:
        resp = requests.get(SIBLING_GLOBE_URL, timeout=10)
        if not resp.ok:
            print(f"[warn] sibling globe returned {resp.status_code}; skipping enrichment")
            return {}
        countries = resp.json().get("countries", [])
        return {
            c["name"]: c["sentiment"] / 80.0
            for c in countries
            if isinstance(c, dict) and isinstance(c.get("sentiment"), (int, float)) and c.get("name")
        }
    except Exception as exc:  # noqa: BLE001 - sibling outage must not break the digest
        print(f"[warn] sibling sentiment unavailable: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Summarize
# ---------------------------------------------------------------------------

def build_llm_input(clusters: list[list[dict]], config: dict) -> str:
    lines = []
    for i, cluster in enumerate(clusters[: config["max_clusters_in_digest"]], 1):
        regions = sorted({it["region"] for it in cluster})
        lines.append(f"STORY {i} (coverage: {len(cluster)} outlets; regions: {', '.join(regions)})")
        for it in cluster[:4]:
            lines.append(f"- [{it['source']}] {it['title']}: {it['summary'][:200]}")
        lines.append("")
    return "\n".join(lines)


def _sentiment_context(clusters: list[list[dict]], config: dict, sentiment: dict) -> str:
    """Render the borrowed per-country tone as an extra prompt-steering block.
    Empty string when no sibling sentiment is available (the common offline path)."""
    if not sentiment:
        return ""
    lines = []
    for i, cluster in enumerate(clusters[: config["max_clusters_in_digest"]], 1):
        countries = _cluster_countries(cluster)
        tones = [sentiment[c] for c in countries if c in sentiment]
        if tones:
            lines.append(f"STORY {i}: avg tone {sum(tones) / len(tones):+.2f} ({', '.join(countries)})")
    if not lines:
        return ""
    return (
        "\n\nSentiment context (borrowed from the sibling sentiment service, "
        "-1 negative .. +1 positive):\n"
        + "\n".join(lines)
        + "\nWhere globally significant, lead with the most negative-tone stories and "
        "note where outlets diverge in tone.\n"
    )


def summarize(clusters: list[list[dict]], config: dict, sentiment: dict | None = None) -> str:
    """Call an LLM to produce the digest. Falls back to extractive mode if no token.
    `sentiment` is optional borrowed per-country tone (-1..1) from the sibling; when
    present it is folded into the prompt as additional steering context."""
    prompt = (
        "You are writing a daily 'State of the World' email digest.\n"
        f"Hard limit: {config['digest_word_limit']} words.\n"
        "Group by theme, lead with the most globally significant stories, "
        "note regional perspective differences where outlets diverge, plain text only.\n\n"
        + build_llm_input(clusters, config)
        + _sentiment_context(clusters, config, sentiment or {})
    )

    hf_token = os.environ.get("HF_TOKEN")
    if hf_token and clusters:
        try:
            resp = requests.post(
                LLM_ENDPOINT,
                headers={"Authorization": f"Bearer {hf_token}"},
                json={
                    "model": LLM_MODEL,
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120,
            )
            if not resp.ok:
                # Surface the provider's own error message, then degrade gracefully
                # rather than crashing the whole run. The body explains the 4xx.
                print(f"[warn] LLM call failed ({resp.status_code}): {resp.text[:500]}")
            else:
                # OpenAI-compatible response shape: choices[0].message.content
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - never let summarization kill the digest
            print(f"[warn] LLM call errored: {exc}")

    # Fallback: extractive digest, keeps the pipeline alive with zero API cost
    lines = [f"State of the World - {datetime.now(timezone.utc):%Y-%m-%d} (extractive mode)\n"]
    for cluster in clusters[: config["max_clusters_in_digest"]]:
        top = cluster[0]
        lines.append(f"* {top['title']} ({top['source']}, {len(cluster)} outlets)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Publish Artifact B (public/digest.json) for the sibling
# ---------------------------------------------------------------------------

def build_digest_payload(clusters: list[list[dict]], config: dict, digest: str) -> dict:
    """Build the news-exchange payload the sibling consumes (contract/news_exchange.md)."""
    out = []
    for cluster in clusters[: config["max_clusters_in_digest"]]:
        top = cluster[0]
        out.append(
            {
                "headline": top["title"],
                "countries": _cluster_countries(cluster),
                "regions": sorted({it["region"] for it in cluster}),
                "outlets": len(cluster),
                "summary": top["summary"][:300],
                "links": [it["link"] for it in cluster[:5] if it["link"]],
            }
        )
    return {
        "schema": EXCHANGE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "digest_words": len(digest.split()),
        "clusters": out,
        "narrative": digest,
    }


def write_digest_json(clusters: list[list[dict]], config: dict, digest: str) -> dict:
    payload = build_digest_payload(clusters, config, digest)
    DIGEST_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_JSON_PATH.write_text(json.dumps(payload, indent=2))
    return payload


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(body: str) -> bool:
    """Send digest via SMTP. Requires env vars: SMTP_USER, SMTP_PASS, DIGEST_TO."""
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("DIGEST_TO")
    if not all([user, password, to_addr]):
        print("[warn] SMTP env vars missing; printing digest instead.\n")
        print(body)
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"State of the World - {datetime.now(timezone.utc):%Y-%m-%d}"
    msg["From"] = user
    msg["To"] = to_addr

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, context=ctx) as srv:
            srv.login(user, password)
            srv.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 - a mail failure must not fail the run
        # e.g. bad Gmail App Password -> SMTPAuthenticationError. Log and record
        # email_sent=False rather than crashing (which would skip metrics commit).
        print(f"[warn] email send failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Run + metrics
# ---------------------------------------------------------------------------

def run() -> dict:
    config = load_config()
    items = fetch_feeds(config)
    clusters = cluster_items(items, config["similarity_threshold"])
    sentiment = fetch_sibling_sentiment()  # best-effort; {} when sibling is offline
    digest = summarize(clusters, config, sentiment)
    sent = send_email(digest)

    payload = write_digest_json(clusters, config, digest)  # publish Artifact B

    regions_covered = sorted({i["region"] for i in items})
    sentiment_enriched = bool(sentiment) and any(
        any(country in sentiment for country in c["countries"]) for c in payload["clusters"]
    )
    metrics = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "items_fetched": len(items),
        "clusters": len(clusters),
        "regions_covered": regions_covered,
        "digest_words": len(digest.split()),
        "email_sent": sent,
        "sibling_reachable": bool(sentiment),
        "sentiment_enriched": sentiment_enriched,
    }
    history = json.loads(METRICS_PATH.read_text()) if METRICS_PATH.exists() else []
    history.append(metrics)
    METRICS_PATH.write_text(json.dumps(history[-90:], indent=2))
    print(json.dumps(metrics, indent=2))
    return metrics


if __name__ == "__main__":
    run()
