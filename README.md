# World Digest — a self-evolving daily news digest

[![Daily Digest](https://github.com/colesr/World.alive/actions/workflows/digest.yml/badge.svg)](https://github.com/colesr/World.alive/actions/workflows/digest.yml)
[![Nightly Evolution](https://github.com/colesr/World.alive/actions/workflows/evolve.yml/badge.svg)](https://github.com/colesr/World.alive/actions/workflows/evolve.yml)

A proof-of-concept self-steering system. Every day it fetches RSS feeds from outlets
across the world, deduplicates the stories, summarizes them with an LLM, and emails
you a "State of the World" digest. Every night, a separate workflow asks an LLM to
propose one improvement to its own pipeline code — which is merged only if the frozen
test suite passes, and auto-reverted otherwise.

## Architecture

```
/app
  core.py          <- the evolvable pipeline (fetch -> cluster -> summarize -> email)
  config.json      <- optional config override (created by evolution over time)
/tests
  test_core.py     <- FROZEN fitness definition; the system can never edit this
/evolution
  evolve.py        <- mutation engine (one change per night)
  prompts.md       <- the goals the mutation LLM is given
  metrics.json     <- performance history (the system's memory)
  attempts.log     <- record of merged/reverted/rejected mutations
/.github/workflows
  digest.yml       <- daily digest run (07:00 UTC)
  evolve.yml       <- nightly evolution run (03:00 UTC)
```

### Why it's built this way

- **Tests are sacred.** `tests/` encodes your intent. CI fails any mutation that
  touches `tests/` or `.github/`. To change the system's goals, *you* edit the tests.
- **One mutation per cycle.** Failures stay diagnosable, reverts stay clean.
- **Git history is memory.** Every accepted/reverted mutation is a commit; the
  mutation LLM sees recent failures so it doesn't repeat them.
- **Hard caps everywhere.** Workflow timeouts, token limits, and a kill switch.

## Setup

1. Create a new GitHub repo and push these files.
2. In repo **Settings → Secrets and variables → Actions**, add:
   - `HF_TOKEN` — a Hugging Face token with "Inference Providers" permission, used
     for summarization + evolution (optional for digest: without it the digest runs
     in free extractive mode)
   - `SMTP_USER` — your Gmail address
   - `SMTP_PASS` — a Gmail **App Password** (Google Account → Security →
     2-Step Verification → App passwords), *not* your real password
   - `DIGEST_TO` — where the digest is sent
3. In repo **Settings → Actions → General → Workflow permissions**, enable
   **Read and write permissions** (so workflows can commit metrics/mutations).
4. Test manually: **Actions → Daily Digest → Run workflow**.

## Kill switch

Create an empty file named `EVOLUTION_PAUSED` in the repo root. Evolution stops
immediately; the daily digest keeps running. Delete the file to resume.

## Your weekly role (~5 minutes)

- Skim the commit log: what did it change, what got reverted?
- Check `evolution/metrics.json` trends: items_fetched, regions_covered, digest_words.
- Occasionally add a new test — that's how you apply new evolutionary pressure.

## Optional: close the feedback loop

Reply to the digest email with a rating like `RATING: 4`. A small script (your first
manual extension!) can read replies via IMAP and append `user_rating` to
`metrics.json`. The evolution prompt already tells the mutation engine to weight
that signal heavily — it turns 10 seconds of your day into directed evolution.

## Local test

```bash
pip install -r requirements.txt
pytest tests/ -q          # fitness check
python app/core.py        # runs the pipeline; prints digest if SMTP unset
```

## Known limits (by design, for a PoC)

- Token-overlap clustering is crude; an obvious evolution target.
- Free-tier API rate limits cap evolution speed to one mutation/night — fine.
- The system will plateau. When it does, study *why* — that's the lesson.
