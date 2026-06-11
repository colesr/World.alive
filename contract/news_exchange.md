# News Exchange Contract — `world-digest/news-exchange@1`

The stable interface between two self-evolving siblings:

- **World Digest** — batch RSS digest pipeline (GitHub Actions, no server). *Publishes* Artifact B; *consumes* Artifact A.
- **InsightsEngine** — live FastAPI app on a Hugging Face Space. *Publishes* Artifact A; *consumes* Artifact B.

Both apps rewrite their own source nightly. This contract is **vendored identically into both repos** (`contract/`) and **pinned by a frozen test on each side**, so it lives outside every evolvable file and no mutation loop can drift it. The schema id (`world-digest/news-exchange@1`) is the version handle — bump it only by editing the frozen tests in both repos.

The cardinal rule on both sides: **the coupling is additive and best-effort.** Each app must fully function with the sibling offline. Enrichment when the sibling is reachable; silent degradation when it is not.

## `contract/country_aliases.json`

Canonical `{ "Country Name": ["lowercased", "alias", ...] }` map (≥40 countries), the shared vocabulary for tagging articles to countries. Matching rule (mirrors InsightsEngine `_match_country_for`): build a lowercased haystack from an article's title (+ summary); a multi-word or punctuated alias matches as a substring, a single-word alias matches on `\b…\b` word boundaries.

## Artifact A — globe sentiment (InsightsEngine → World Digest)

Already produced by `GET /api/news/globe`. World Digest reads it best-effort (env `SIBLING_GLOBE_URL`, default the live Space) to borrow sentiment without computing its own. Consumed fields:

```
countries: [ { name: str, sentiment: float (-80..80), articles: int, ... }, ... ]
```

World Digest normalizes `sentiment / 80` → `-1..1` per country.

## Artifact B — digest narrative (World Digest → InsightsEngine)

World Digest writes `public/digest.json` and commits it; InsightsEngine reads it best-effort (env `DIGEST_JSON_URL`, default `https://raw.githubusercontent.com/colesr/World.alive/main/public/digest.json`).

```json
{
  "schema": "world-digest/news-exchange@1",
  "generated_at": "<ISO-8601 UTC>",
  "digest_words": 0,
  "clusters": [
    {
      "headline": "string — top story title in the cluster",
      "countries": ["United States"],
      "regions": ["Americas"],
      "outlets": 3,
      "summary": "string — short gloss of the cluster",
      "links": ["https://..."]
    }
  ],
  "narrative": "string — the full LLM digest text"
}
```

`countries[]` uses the canonical names from `country_aliases.json`. On any fetch failure InsightsEngine returns the same shape with `clusters: []`, `narrative: ""`, and `stale: true`.
