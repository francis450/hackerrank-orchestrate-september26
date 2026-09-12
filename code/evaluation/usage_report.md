# Token Usage and Cost Report

Final full-dataset run that produced `output.csv`.

## Provider and model

| Provider | Model | Role |
|---|---|---|
| anthropic | `claude-sonnet-5` | Message classification and image amount extraction |

The decision engine itself is deterministic and makes **no** model calls. The
model is used only to turn `messages.csv` and `media/images/*.png` into
schema-validated facts; every amount, date and plan in `output.csv` is computed
by the solver.

## Totals

| Metric | Value |
|---|---|
| Model calls | 231 |
| Input tokens | 308,977 |
| Output tokens | 10,571 |
| Total tokens | 319,548 |
| Estimated total cost | $0.7237 |

## Per request

Evaluation requests in `dataset/requests.csv`: **250**

| Metric | Value |
|---|---|
| Average tokens per request | 1,278.2 |
| Average model calls per request | 0.924 |
| Estimated cost per request | $0.002895 |

## Per model

| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Cost (USD) |
|---|---|---|---|---|---|---|
| anthropic | `claude-sonnet-5` | 231 | 308,977 | 10,571 | 319,548 | $0.723664 |

Pricing: `claude-sonnet-5` at $2.00 / 1M input tokens and $10.00 / 1M output tokens.

## Caching

Extractions are cached in `code/facts_cache/facts.json`, keyed by `message_id`
and `image_id`. The 231 calls above cover all 215 messages and 16
images exactly once. A re-run of the full dataset makes **zero** API calls and
costs **$0.00**; the figures above are the one-time cost of building the cache.

No API keys, credentials, or sensitive configuration are included in this report.
