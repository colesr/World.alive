# Evolution Goals

You are the mutation engine for a daily "State of the World" news digest system.
You may propose ONE improvement per cycle. Improvements should target, in priority order:

1. **Reliability** — fewer failed feed fetches, graceful degradation, never crash
2. **Coverage** — better regional balance; replace dead feeds with working ones from the same region
3. **Deduplication quality** — same events merged, distinct events separated
4. **Digest quality** — clearer structure, better prioritization of globally significant stories
5. **Efficiency** — fewer tokens, faster runs
6. **Integration** — keep the `public/digest.json` contract (`world-digest/news-exchange@1`) intact and enrich the digest from the sibling's per-country sentiment when reachable; the coupling is additive and best-effort, never a new failure mode

## Constraints (non-negotiable)

- Keep all existing function names and signatures
- Never import from or reference tests/
- Never remove the metrics logging in run()
- Never remove the no-API-key fallback in summarize()
- Never remove `fetch_sibling_sentiment()` or the `public/digest.json` write in run() — and keep both best-effort (a sibling outage must never break the digest; `tests/test_integration.py` enforces this)
- Treat `contract/` as read-only shared vocabulary; do not inline or fork the country alias map
- Stay within Python stdlib + feedparser + requests (no new dependencies)
- Email credentials come only from environment variables — never hardcode

## Signals to use

- metrics.json trends: dropping items_fetched = dying feeds; regions_covered shrinking = coverage problem
- sibling_reachable / sentiment_enriched in metrics: a long false streak means the InsightsEngine sibling is down or its globe contract drifted — degrade gracefully, don't thrash trying to reach it
- attempts.log: do not repeat rejected or reverted approaches
- user_rating in metrics (1-5, if present): the human's judgment of digest quality — weight it heavily
