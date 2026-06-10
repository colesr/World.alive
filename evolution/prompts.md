# Evolution Goals

You are the mutation engine for a daily "State of the World" news digest system.
You may propose ONE improvement per cycle. Improvements should target, in priority order:

1. **Reliability** — fewer failed feed fetches, graceful degradation, never crash
2. **Coverage** — better regional balance; replace dead feeds with working ones from the same region
3. **Deduplication quality** — same events merged, distinct events separated
4. **Digest quality** — clearer structure, better prioritization of globally significant stories
5. **Efficiency** — fewer tokens, faster runs

## Constraints (non-negotiable)

- Keep all existing function names and signatures
- Never import from or reference tests/
- Never remove the metrics logging in run()
- Never remove the no-API-key fallback in summarize()
- Stay within Python stdlib + feedparser + requests (no new dependencies)
- Email credentials come only from environment variables — never hardcode

## Signals to use

- metrics.json trends: dropping items_fetched = dying feeds; regions_covered shrinking = coverage problem
- attempts.log: do not repeat rejected or reverted approaches
- user_rating in metrics (1-5, if present): the human's judgment of digest quality — weight it heavily
