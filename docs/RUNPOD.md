# Photo assessment on RunPod Serverless

car-hunter sends listing photos to a vision-language model running on a RunPod Serverless endpoint
with the official [vLLM worker](https://github.com/runpod-workers/worker-vllm). The endpoint exposes
an OpenAI-compatible API, so no custom Docker image is needed. You pay only for GPU seconds;
the endpoint scales to zero when idle.

## 1. Create the endpoint

RunPod console → **Serverless** → **New Endpoint** → **vLLM** (quick deploy, uses the latest worker image).

| Setting | Value |
|---|---|
| Model | `Qwen/Qwen3-VL-8B-Instruct-FP8` |
| GPU | 24 GB tier (L4 / RTX 4090 / A5000) |
| Max workers | 2 |
| Active (min) workers | 0 |
| Idle timeout | 60 s |
| Execution timeout | 600 s |
| FlashBoot | on |

Environment variables (Edit endpoint → Environment Variables):

```env
MODEL_NAME=Qwen/Qwen3-VL-8B-Instruct-FP8
MAX_MODEL_LEN=16384
GPU_MEMORY_UTILIZATION=0.92
MAX_NUM_SEQS=8
LIMIT_MM_PER_PROMPT={"image":12}
```

If requests with many photos fail with a "too many images" error, the worker did not pass
`LIMIT_MM_PER_PROMPT` through. Remove it and set instead:

```env
VLLM_EXTRA_ARGS=--limit-mm-per-prompt {"image":12}
```

Copy the **Endpoint ID** from the endpoint page and create an API key under
**Settings → API Keys** (read/write for Serverless).

## 2. Smoke test

```bash
curl -s https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/openai/v1/chat/completions \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-VL-8B-Instruct-FP8","max_tokens":60,
       "messages":[{"role":"user","content":[
         {"type":"text","text":"Describe this image in one sentence."},
         {"type":"image_url","image_url":{"url":"https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png"}}]}]}'
```

The first call includes a cold start (model download and load), which can take a few minutes.
Later calls within the idle timeout are fast.

## 3. Enable in car-hunter

Add to `.env` and restart:

```env
RUNPOD_API_KEY=...
RUNPOD_ENDPOINT_ID=...
```

```bash
docker compose up -d
docker compose run --rm hunter top -n 10            # pick an id
docker compose run --rm hunter vision --id 123      # assess one listing, print JSON + estimate
```

By default photo assessment is **manual only** (`vision.auto: false`): nothing is sent unless
you run `hunter vision`. Preview the queue first, at no cost:

```bash
docker compose run --rm hunter vision --dry --limit 5
docker compose run --rm hunter vision --limit 3
```

After checking cost and quality, set `vision.auto: true`. Every cycle then runs
`evaluate → vision (queue) → evaluate → alert`, within `max_per_run` and `max_per_day`.

## What is sent

Only listings that can still be a deal:

- active candidate with at least `vision.min_photos` photos (OtoMoto details already fetched),
- not right-hand drive, meets the power criterion,
- `market value − (price + fees + 2 500 PLN) ≥ vision.min_optimistic_savings_pln`,
- `price + fees + 2 500 PLN ≤ budget`,
- not assessed yet (errors are retried after 24 h).

The queue is ordered by price gap to market and capped by `vision.max_per_run` and
`vision.max_per_day`. With `vision.auto: true` and `vision.require_for_alert: true`, Telegram alerts are sent only
for listings assessed from photos.

## How the estimate is built

The model only classifies, constrained by a JSON schema (`RESPONSE_SCHEMA` in `hunter/vision.py`):
damaged zones, parts to replace or repair, airbags, structural damage, suspension, flood and fire
signs, drivability, which sides of the car are shown, whether photos match the description, and a
short note in Polish.

The repair cost is then computed locally:

```
repair = (Σ part costs + airbags + ADAS calibration + geometry/diagnostics)
         × segment factor (√(market value / 70 000), 0.75–1.6)
         × (1 + hidden-damage contingency)
```

The contingency is 15%, plus 5% for every side of the car not shown and 10% when structural damage
is likely. Part costs (`PART_COSTS`) are workshop prices with aftermarket or used parts for a
mid-segment car. Calibrate them against real quotes.

## Cost

Each listing is one request with up to 12 images at 1024×768, roughly 10k prompt tokens.
On a 24 GB GPU this takes a few seconds, and concurrent requests are batched by vLLM. Expect
well under 1 US cent per listing plus cold starts; check actual usage on the RunPod billing page
after the first runs and adjust `max_per_day`.
