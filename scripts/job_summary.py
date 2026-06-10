"""
Render a GitHub Actions job summary (markdown) for digest / evolution runs.

Turns each run into a readable dashboard in the Actions UI instead of buried
log lines. Writes markdown to the file named by $GITHUB_STEP_SUMMARY (set by
GitHub Actions), or to stdout when run locally.

Usage:
    python scripts/job_summary.py digest [captured_stdout.txt]
    python scripts/job_summary.py evolve
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS = ROOT / "evolution" / "metrics.json"
ATTEMPTS = ROOT / "evolution" / "attempts.log"


def out(line: str = "") -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    else:
        print(line)


def last_metrics() -> dict:
    if METRICS.exists():
        history = json.loads(METRICS.read_text())
        if history:
            return history[-1]
    return {}


def digest_summary(capture_path: str | None) -> None:
    m = last_metrics()
    out("## 🌍 Daily digest run\n")
    if m:
        out("| metric | value |")
        out("|---|---|")
        for key in ["timestamp", "items_fetched", "clusters", "digest_words", "email_sent"]:
            out(f"| {key} | {m.get(key, '—')} |")
        out(f"| regions_covered | {', '.join(m.get('regions_covered', [])) or '—'} |")
    else:
        out("_No metrics recorded — the pipeline may have failed before writing._")

    notices = []
    if capture_path:
        p = Path(capture_path)
        if p.exists():
            notices = [
                ln.strip()
                for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                if "[warn]" in ln or "[info]" in ln or "[error]" in ln
            ]
    out("\n### Notices\n")
    if notices:
        for n in notices:
            out(f"- `{n}`")
    else:
        out("_None — all feeds and delivery succeeded._")


def evolve_summary() -> None:
    out("## 🧬 Nightly evolution run\n")
    if ATTEMPTS.exists():
        recent = [ln for ln in ATTEMPTS.read_text().splitlines() if ln.strip()][-4:]
        out("Most recent attempts (newest last):\n")
        for ln in recent:
            out(f"- `{ln}`")
    else:
        out("_No attempts logged yet._")
    m = last_metrics()
    if m:
        out(
            f"\nLatest digest health: {m.get('items_fetched', '—')} items, "
            f"{len(m.get('regions_covered', []))} regions, "
            f"digest_words={m.get('digest_words', '—')}."
        )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "digest"
    if mode == "evolve":
        evolve_summary()
    else:
        digest_summary(sys.argv[2] if len(sys.argv) > 2 else None)


if __name__ == "__main__":
    main()
