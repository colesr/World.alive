"""
Evolution engine: proposes ONE mutation to app/core.py per run.

Cycle:
  1. Read current core.py, metrics history, and past failed attempts
  2. Ask LLM for one targeted improvement (full rewritten file)
  3. Write candidate to app/core.py (in a branch - handled by CI workflow)
  4. CI runs tests; merge or revert is decided by the workflow, not this script

Hard rules:
  - Never touches tests/ (CI also enforces this)
  - One mutation per run
  - Logs every attempt (success or failure) to evolution/attempts.log
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
CORE = ROOT / "app" / "core.py"
METRICS = ROOT / "evolution" / "metrics.json"
ATTEMPTS = ROOT / "evolution" / "attempts.log"
PROMPTS = ROOT / "evolution" / "prompts.md"

MAX_TOKENS = 8000  # hard budget cap per run


def recent_metrics(n=7) -> str:
    if not METRICS.exists():
        return "No metrics yet."
    history = json.loads(METRICS.read_text())
    return json.dumps(history[-n:], indent=2)


def recent_failures(n=5) -> str:
    if not ATTEMPTS.exists():
        return "No previous attempts."
    lines = ATTEMPTS.read_text().strip().splitlines()
    return "\n".join(lines[-n:])


def propose_mutation() -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[error] ANTHROPIC_API_KEY not set; cannot evolve.")
        return None

    goals = PROMPTS.read_text() if PROMPTS.exists() else ""
    prompt = f"""{goals}

CURRENT CODE (app/core.py):
```python
{CORE.read_text()}
```

RECENT METRICS (last 7 runs):
{recent_metrics()}

RECENT FAILED ATTEMPTS (do not repeat these):
{recent_failures()}

Propose exactly ONE improvement to core.py. Output the COMPLETE rewritten file
inside a single ```python code block, and nothing else. The file must keep all
existing function names and signatures (load_config, fetch_feeds, cluster_items,
summarize, send_email, run). Do not import from tests/."""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=300,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"]

    # Extract the code block
    if "```python" in text:
        code = text.split("```python", 1)[1].split("```", 1)[0].strip()
        return code + "\n"
    return None


def log_attempt(status: str, note: str = ""):
    ATTEMPTS.parent.mkdir(exist_ok=True)
    with ATTEMPTS.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} | {status} | {note}\n")


def main():
    code = propose_mutation()
    if not code:
        log_attempt("SKIPPED", "no valid code block returned")
        sys.exit(1)

    # Refuse anything that smells like fitness gaming
    forbidden = ["tests/", "test_core", "pytest.skip", "os.remove", "shutil.rmtree"]
    for token in forbidden:
        if token in code:
            log_attempt("REJECTED", f"forbidden token: {token}")
            sys.exit(1)

    CORE.write_text(code)
    log_attempt("PROPOSED", f"{len(code)} chars written; CI will validate")
    print("Mutation written. CI will test and merge or revert.")


if __name__ == "__main__":
    main()
