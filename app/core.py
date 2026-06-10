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
        ],
    },
    "max_items_per_feed": 15,
    "max_clusters_in_digest": 12,
    "digest_word_limit": 800,
    "similarity_threshold": 0.45,
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


def cluster_items(items: list[dict], threshold: float) -> list[list[dict]]:
    """Greedy clustering by title-token Jaccard similarity."""
    clusters: list[dict] = []  # each: {"tokens": set, "items": [..]}
    for item in items:
        toks = _tokens(item["title"])
        best, best_score = None, 0.0
        for cluster in clusters:
            score = _similarity(toks, cluster["tokens"])
            if score > best_score:
                best, best_score = cluster, score
        if best is not None and best_score >= threshold:
            best["items"].append(item)
            best["tokens"] |= toks
        else:
            clusters.append({"tokens": set(toks), "items": [item]})
    # Bigger clusters first = more widely covered = more important
    clusters.sort(key=lambda c: len(c["items"]), reverse=True)
    return [c["items"] for c in clusters]


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


def summarize(clusters: list[list[dict]], config: dict) -> str:
    """Call an LLM to produce the digest. Falls back to extractive mode if no token."""
    prompt = (
        "You are writing a daily 'State of the World' email digest.\n"
        f"Hard limit: {config['digest_word_limit']} words.\n"
        "Group by theme, lead with the most globally significant stories, "
        "note regional perspective differences where outlets diverge, plain text only.\n\n"
        + build_llm_input(clusters, config)
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
    digest = summarize(clusters, config)
    sent = send_email(digest)

    regions_covered = sorted({i["region"] for i in items})
    metrics = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "items_fetched": len(items),
        "clusters": len(clusters),
        "regions_covered": regions_covered,
        "digest_words": len(digest.split()),
        "email_sent": sent,
    }
    metrics_file = Path(__file__).parent.parent / "evolution" / "metrics.json"
    history = json.loads(metrics_file.read_text()) if metrics_file.exists() else []
    history.append(metrics)
    metrics_file.write_text(json.dumps(history[-90:], indent=2))
    print(json.dumps(metrics, indent=2))
    return metrics


if __name__ == "__main__":
    run()
