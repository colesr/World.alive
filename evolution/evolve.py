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
import re
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

# Mutation engine runs on Hugging Face's free OpenAI-compatible router, authed
# with an `hf_...` token (HF_TOKEN) that has "Inference Providers" permission.
# A code-specialized model writes the full-file rewrite; ":cheapest" picks the
# lowest-cost provider serving it.
LLM_ENDPOINT = "https://router.huggingface.co/v1/chat/completions"
LLM_MODEL = "Qwen/Qwen2.5-Coder-32B-Instruct:cheapest"


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
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        print("[error] HF_TOKEN not set; cannot evolve.")
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
        LLM_ENDPOINT,
        headers={"Authorization": f"Bearer {hf_token}"},
        json={
            "model": LLM_MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=300,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]  # OpenAI-compatible shape

    # Extract the rewritten file. Models vary in how they fence code, so accept a
    # ```python block, any ``` block, or (last resort) a reply that is itself the
    # file. Print a preview if none match so the failure is diagnosable in logs.
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    stripped = text.strip()
    if stripped.startswith(('"""', "'''", "import ", "from ", "#")):
        return stripped + "\n"
    print(f"[error] no code block found in {len(text)}-char response. Preview:\n{text[:600]}")
    return None


def log_attempt(status: str, note: str = ""):
    ATTEMPTS.parent.mkdir(exist_ok=True)
    with ATTEMPTS.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} | {status} | {note}\n")


def main():
    code = propose_mutation()
    if not code:
        print("[skip] model returned no usable code")
        log_attempt("SKIPPED", "no valid code block returned")
        sys.exit(1)

    # Refuse anything that smells like fitness gaming. Match real code patterns,
    # not bare substrings: core.py's own docstring mentions "tests/test_core.py",
    # so guarding on "tests/" / "test_core" would reject every faithful rewrite.
    # The frozen tests and the CI `git diff -- tests/ .github/` guard are the real
    # enforcement; this is just a fast pre-check for obvious gaming.
    forbidden = ["import tests", "from tests", "pytest.skip", "os.remove", "shutil.rmtree"]
    for token in forbidden:
        if token in code:
            print(f"[reject] mutation contains forbidden pattern: {token!r}")
            log_attempt("REJECTED", f"forbidden token: {token}")
            sys.exit(1)

    CORE.write_text(code)
    log_attempt("PROPOSED", f"{len(code)} chars written; CI will validate")
    print("Mutation written. CI will test and merge or revert.")


if __name__ == "__main__":
    main()
