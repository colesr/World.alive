# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

World Digest is a self-evolving daily news digest. Two scheduled GitHub Actions run it:
- **Daily** (`digest.yml`, 07:00 UTC): `app/core.py` fetches RSS feeds → clusters duplicate stories → summarizes with an LLM → emails a "State of the World" digest, then commits `evolution/metrics.json`.
- **Nightly** (`evolve.yml`, 03:00 UTC): `evolution/evolve.py` asks an LLM to rewrite `app/core.py` with one improvement. CI merges the mutation only if `tests/test_core.py` passes, and `git checkout` reverts it otherwise.

## Commands

```bash
pip install -r requirements.txt
pytest tests/ -q                 # the fitness check — gates every mutation
pytest tests/test_core.py::test_duplicate_stories_are_clustered  # single test
python app/core.py               # run the pipeline; prints digest if SMTP env unset
python evolution/evolve.py       # propose one mutation (needs HF_TOKEN)
```

Runtime env vars (all optional locally): `HF_TOKEN` (a Hugging Face token with "Inference Providers" permission; without it, `summarize()` falls back to extractive mode), `SMTP_USER` / `SMTP_PASS` / `DIGEST_TO` (without them, the digest prints instead of emailing).

## The core invariant

`tests/test_core.py` is the **frozen fitness definition** and the user's encoded intent. The evolution loop is only correct because this file (and `.github/`) can never be modified by the system. This is enforced in three layers:
1. `evolve.yml` runs `git diff --quiet -- tests/ .github/` and reverts + fails if either changed.
2. `evolve.py` rejects any candidate `core.py` containing forbidden tokens (`import tests`, `from tests`, `pytest.skip`, `os.remove`, `shutil.rmtree`). Note it deliberately does **not** guard on bare `tests/` or `test_core` — `core.py`'s own docstring mentions `tests/test_core.py`, so those would reject every faithful rewrite. This pre-check is a fast smell test; layers 1 and 3 are the real enforcement.
3. `tests/test_core.py` itself asserts `core.py` never imports from `tests/`.

**To change the system's behavior or goals, edit the tests** — that is the intended steering mechanism. Do not weaken or delete tests to make a mutation pass; that defeats the entire design.

## Working on `app/core.py`

It is the only evolvable file. Any change must keep these function names and signatures intact (the tests and the mutation prompt both depend on them): `load_config`, `fetch_feeds`, `cluster_items`, `summarize`, `send_email`, `run`. Also preserve, per `evolution/prompts.md`:
- the no-API-key extractive fallback in `summarize()`
- the metrics logging in `run()` (appends to `evolution/metrics.json`, capped at last 90 runs)
- stdlib + `feedparser` + `requests` only — no new dependencies
- config is overridable at runtime via `app/config.json`; defaults live in `DEFAULT_CONFIG`

Clustering is greedy Jaccard similarity over title tokens (`cluster_items`), sorted largest-cluster-first since broader coverage implies more global significance.

`scripts/job_summary.py` is a CI-only helper (not part of the pipeline): both workflows call it to render a markdown run dashboard to `$GITHUB_STEP_SUMMARY` from `metrics.json` / `attempts.log`. It is not frozen but nothing in the evolution loop touches it.

## Steering the evolution engine

- `evolution/prompts.md` is the goal hierarchy handed to the mutation LLM (reliability > coverage > dedup > digest quality > efficiency). Edit it to redirect what mutations optimize for.
- The mutation LLM is shown recent `metrics.json` trends and recent `attempts.log` entries so it doesn't repeat reverted approaches.
- **Kill switch:** an empty `EVOLUTION_PAUSED` file in the repo root halts evolution (the daily digest keeps running). Delete it to resume.
- Both LLM calls run on the Hugging Face Inference Providers router via `HF_TOKEN`, pinned inline as `LLM_ENDPOINT` / `LLM_MODEL` constants: `core.py` summarizes with `meta-llama/Llama-3.3-70B-Instruct:cheapest`; `evolve.py` mutates with the code-specialized `Qwen/Qwen3-Coder-480B-A35B-Instruct:cheapest`. Both use the OpenAI-compatible response shape (`choices[0].message.content`).

## Maintaining this live system

This repo edits itself nightly, so before changing anything, identify **which of three surfaces** you are touching — each has a different procedure:

| Surface | Files | Changed by | How |
|---|---|---|---|
| **Frozen / steering** | `tests/`, `.github/` | Humans only (CI blocks the bot) | PR. This encodes intent. |
| **Human-authored** | `evolution/evolve.py`, `evolution/prompts.md`, `scripts/`, `requirements.txt`, `app/config.json`, docs | Humans only (the bot structurally only writes `core.py`) | Normal PR. |
| **Self-evolving** | `app/core.py` **only** | The nightly bot, and humans (carefully) | Mutation, gated by tests. |

**Golden rule: prefer not to hand-edit `app/core.py`.** Anything polished there by hand can be silently undone by the next mutation. To make an improvement *stick*, write a **test** that demands it and let the loop grow `core.py` toward it — steering is done through tests, not code.

Routing a change:
- **Lock in a quality bar / new behavior** → add or tighten a test in `tests/test_core.py`; the loop adapts over the next few nights.
- **Redirect what mutations chase** → edit `evolution/prompts.md`.
- **Feature outside the pipeline** (e.g. an IMAP reply-rating reader, dashboards, alerting) → new file under `scripts/`, human-authored PR; never touches the evolution loop.
- **Feeds/regions** → assert the requirement in a test (see `test_every_region_has_redundant_feeds`) or set it via `app/config.json` (the bot reads it, you own it), rather than hand-editing `DEFAULT_CONFIG`.
- **Refactor `core.py` by hand / swap a model constant** → the one risky case; pause evolution first (below).

Working alongside the bots — `main` receives commits from **digest-bot** (metrics, daily 07:00 UTC) and **evolution-bot** (`core.py` + `attempts.log`, nightly 03:00 UTC):
1. `git pull` before every work session — local `main` goes stale within a day.
2. Hand-editing `core.py`? Commit an empty `EVOLUTION_PAUSED` file first, do the work + PR, then delete it — otherwise you race the nightly mutation and conflict.
3. Don't commit local `metrics.json` churn — `python app/core.py` appends to it; stage files explicitly, never `git add -A`.
4. Keep PR branches short-lived, especially any touching `core.py` or `metrics.json`.

Health signals to watch: repeated `REVERTED` entries for the same idea in `attempts.log` mean a test or prompt is mis-shaped (intervene); a flat `metrics.json` trend means a plateau (add a sharper test). Tests gate structure and safety but **cannot gate digest writing quality** — a mutation can pass every test and still produce a worse email, so that gap is watched by a human actually reading the output.
