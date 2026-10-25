# car-hunter

**Find the underpriced damaged car worth fixing.** car-hunter watches every damaged car listed on
[OtoMoto](https://www.otomoto.pl) and [OLX](https://www.olx.pl) (Poland's two biggest car marketplaces),
values each one against the market for the same car in good condition, estimates the repair cost and
risk, and sends the best deals to Telegram.

It is built to find a car to repair and drive yourself, not to flip. The ranking is based on how much
you save compared with buying an undamaged one, adjusted for risk.

🇵🇱 [Polska wersja README](README.pl.md)

## Features

- **Whole-market coverage.** No model whitelist: every damaged car that matches your criteria is tracked.
- **Two sources, deduplicated.** OtoMoto (embedded Next.js state) plus OLX (public JSON API).
  OLX listings mirrored from OtoMoto are skipped.
- **Market valuation.** Undamaged listings of the same make and model are collected automatically.
  The price is estimated with a log-price regression on year, mileage and power, falling back to a
  weighted median of the nearest comparables when the sample is small.
- **Repair and risk estimate.** Damage severity, US imports (Copart/IAAI), flood damage, missing Polish
  registration, few photos.
- **Full cost.** Purchase price + repair + 2% PCC tax + inspection and registration + transport,
  checked against your total budget.
- **Price history.** Every price change is stored, so price drops are visible.
- **Telegram alerts** for new listings above a score threshold.
- **Polite scraping.** Browser TLS fingerprint, 2.5–6 s random delay per host, backoff on 403/429,
  incremental runs that stop as soon as only known listings show up.

## How it works

```
candidates → enrich → market → evaluate → vision → evaluate → alert
damaged      VIN,      undamaged  valuation   photos on  repair from  Telegram
cars         photos,   cars for   + keyword   RunPod     parts table
             text      valuation  estimate    (Qwen3-VL)
```

```
savings = market_value × 0.93 − (price + repair + fees)
score   = savings × (1 − risk) + preference bonus
```

The 0.93 factor converts asking prices into realistic transaction prices.

## Default criteria

Set in `config.yaml` (copy it from `config.example.yaml`):

| Setting | Default |
|---|---|
| Gearbox | automatic only |
| Engine power | ≥ 170 HP |
| Total budget (purchase + repair + fees) | 55 000 PLN |
| Max asking price | 50 000 PLN |
| Preferred (bonus, not required) | SUV / estate, diesel |
| Excluded | right-hand drive |

## Quick start

Requires Docker with Compose. Runs well in a Proxmox LXC or on any small home server.

```bash
git clone https://github.com/mice6/car-hunter.git && cd car-hunter
cp config.example.yaml config.yaml
cp .env.example .env          # DB password, Telegram bot token and chat id
docker compose up -d --build
docker compose logs -f hunter
```

The first run does a full sweep and builds the market dataset, which takes a few hours because
requests are deliberately slow. After that, a 30-minute incremental cycle runs automatically.

### Manual commands

```bash
docker compose run --rm hunter candidates --full   # fetch damaged cars (full sweep)
docker compose run --rm hunter enrich --limit 300  # VIN, all photos, description (OtoMoto)
docker compose run --rm hunter market              # undamaged comparables for valuation
docker compose run --rm hunter evaluate            # value, estimate, score
docker compose run --rm hunter top -n 20           # print the ranking
docker compose run --rm hunter vision --id 123      # photo assessment for one listing (prints JSON)
docker compose run --rm hunter vision              # photo assessment for the queue
docker compose run --rm hunter alert               # send Telegram alerts
```

PostgreSQL is exposed on `127.0.0.1:5433` for DBeaver, Metabase or n8n.

## Project layout

```
hunter/
  sources/otomoto.py   OtoMoto search + listing detail parser
  sources/olx.py       OLX API client, make discovery, price-band pagination
  scoring.py           valuation, damage/risk heuristics, scoring
  vision.py            RunPod/vLLM client, prompt + JSON schema, parts-based repair estimate
  pipeline.py          candidates → enrich → market → evaluate → alert
  db.py                upserts, price history, detail merge
  http.py              throttled HTTP client with backoff
  cli.py               CLI and scheduler loop
sql/schema.sql         database schema
tests/                 parser, scoring and DB tests
```

## Tests

```bash
pip install -r requirements.txt pytest
pytest -q                                   # parsers and scoring
HUNTER_TEST_DB=postgresql://... pytest -q   # + database layer (drops the public schema!)
```

## Roadmap

- [x] OtoMoto + OLX scraper, price history, comparable-based valuation, scoring, Telegram alerts
- [ ] **Global valuation model** (CatBoost) trained on the whole undamaged market, including trim,
      equipment and region. Values rare trims and also flags underpriced undamaged cars.
- [x] **Photo damage assessment on RunPod Serverless** ([setup guide](docs/RUNPOD.md)). Qwen3-VL reviews
      up to 12 photos per listing and returns schema-constrained JSON: damaged zones, parts to replace
      or repair, airbags, structural damage, flood/fire signs, drivability, photo coverage and whether
      the photos match the description. Repair cost is computed from a parts price table, not by the
      model. Only listings that can still be a deal are sent, with a daily cap.
- [ ] **Deep review of the top ~5% with Claude.** Finds contradictions between description and photos
      and drafts questions for the seller.
- [ ] Parts price lookup (OLX/Allegro) for repair cost estimates
- [ ] Dashboard with ranking and price history

## Limitations

- Without a RunPod endpoint, the repair estimate is a keyword heuristic over the title and description.
  With photo assessment enabled, the parts price table (`hunter/vision.py`) still needs calibration
  against real repair quotes.
- Site structure was verified on 2026-10-24. Marketplaces change their frontends; when parsing breaks,
  the tests in `tests/` point to the affected parser.
- The output is a deal filter, not a buying decision. Always check the VIN history (including US
  auction records), inspect the car, measure paint thickness and run diagnostics.

## Disclaimer

This is a personal project for individual use. OtoMoto and OLX terms of service restrict automated
data collection. Keep request rates low, do not redistribute the collected data, and use it at your
own risk. Not affiliated with OtoMoto, OLX or Grupa OLX.

## License

MIT
