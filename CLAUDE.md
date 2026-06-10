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
python evolution/evolve.py       # propose one mutation (needs ANTHROPIC_API_KEY)
```

Runtime env vars (all optional locally): `HF_TOKEN` (a Hugging Face token with "Inference Providers" permission; without it, `summarize()` falls back to extractive mode), `SMTP_USER` / `SMTP_PASS` / `DIGEST_TO` (without them, the digest prints instead of emailing).

## The core invariant

`tests/test_core.py` is the **frozen fitness definition** and the user's encoded intent. The evolution loop is only correct because this file (and `.github/`) can never be modified by the system. This is enforced in three layers:
1. `evolve.yml` runs `git diff --quiet -- tests/ .github/` and reverts + fails if either changed.
2. `evolve.py` rejects any candidate `core.py` containing forbidden tokens (`tests/`, `test_core`, `pytest.skip`, `os.remove`, `shutil.rmtree`).
3. `tests/test_core.py` itself asserts `core.py` never imports from `tests/`.

**To change the system's behavior or goals, edit the tests** — that is the intended steering mechanism. Do not weaken or delete tests to make a mutation pass; that defeats the entire design.

## Working on `app/core.py`

It is the only evolvable file. Any change must keep these function names and signatures intact (the tests and the mutation prompt both depend on them): `load_config`, `fetch_feeds`, `cluster_items`, `summarize`, `send_email`, `run`. Also preserve, per `evolution/prompts.md`:
- the no-API-key extractive fallback in `summarize()`
- the metrics logging in `run()` (appends to `evolution/metrics.json`, capped at last 90 runs)
- stdlib + `feedparser` + `requests` only — no new dependencies
- config is overridable at runtime via `app/config.json`; defaults live in `DEFAULT_CONFIG`

Clustering is greedy Jaccard similarity over title tokens (`cluster_items`), sorted largest-cluster-first since broader coverage implies more global significance.

## Steering the evolution engine

- `evolution/prompts.md` is the goal hierarchy handed to the mutation LLM (reliability > coverage > dedup > digest quality > efficiency). Edit it to redirect what mutations optimize for.
- The mutation LLM is shown recent `metrics.json` trends and recent `attempts.log` entries so it doesn't repeat reverted approaches.
- **Kill switch:** an empty `EVOLUTION_PAUSED` file in the repo root halts evolution (the daily digest keeps running). Delete it to resume.
- Both LLM calls run on the Hugging Face Inference Providers router via `HF_TOKEN`, pinned inline as `LLM_ENDPOINT` / `LLM_MODEL` constants: `core.py` summarizes with `meta-llama/Llama-3.3-70B-Instruct:cheapest`; `evolve.py` mutates with the code-specialized `Qwen/Qwen3-Coder-480B-A35B-Instruct:cheapest`. Both use the OpenAI-compatible response shape (`choices[0].message.content`).
