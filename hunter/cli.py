from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

from . import db, pipeline
from .config import load_config
from .notify import fmt_pln

log = logging.getLogger("hunter")


def _print_top(rows):
    if not rows:
        print("Brak kandydatów spełniających kryteria (uruchom: candidates, enrich, market, evaluate).")
        return
    for i, r in enumerate(rows, 1):
        flags = "; ".join(((r.get("details") or {}).get("damage") or {}).get("flags") or [])
        print(f"{i:>2}. {r['title'][:45]:<45} {r['year'] or '':>4} {r['power_hp'] or '':>4}KM "
              f"cena {fmt_pln(r['price_pln']):>11} | rynek {fmt_pln(r['market_value']):>11} ({r['comparables_n']}) "
              f"| naprawa {fmt_pln(r['repair_estimate']):>10} | oszcz. {fmt_pln(r['savings']):>11} "
              f"| ryzyko {float(r['risk'] or 0):.0%} | score {fmt_pln(r['score'])}")
        if flags:
            print(f"    ⚠ {flags}")
        print(f"    {r['url']}")


def _due(conn, key: str, hours: float) -> bool:
    v = db.kv_get(conn, key)
    if not v:
        return True
    try:
        last = datetime.fromisoformat(v)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last > timedelta(hours=hours)


def _mark(conn, key: str) -> None:
    db.kv_set(conn, key, datetime.now(timezone.utc).isoformat())


def run_loop(cfg, conn):
    sch = cfg.schedule
    while True:
        started = time.monotonic()
        http = pipeline.make_http(cfg)
        try:
            full = _due(conn, "last_full_sweep", sch["full_sweep_every_hours"])
            pipeline.scrape_candidates(cfg, conn, http, full=full)
            pipeline.enrich(cfg, conn, http)
            if _due(conn, "last_market", sch["market_every_hours"]):
                pipeline.refresh_market(cfg, conn, http)
                db.deactivate_stale(conn, "market", 24 * 14)
                _mark(conn, "last_market")
            pipeline.evaluate_all(cfg, conn)
            pipeline.alert(cfg, conn)
        except Exception:
            log.exception("cycle failed")
        log.info("cycle done in %.0fs, %d requests", time.monotonic() - started, http.requests_made)
        sleep_s = max(60, sch["candidates_every_min"] * 60 - (time.monotonic() - started))
        time.sleep(sleep_s)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hunter", description="Monitor uszkodzonych aut OtoMoto/OLX")
    ap.add_argument("--config")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    c = sub.add_parser("candidates", help="pobierz uszkodzone auta spełniające kryteria")
    c.add_argument("--full", action="store_true", help="pełny przebieg (inaczej przyrostowo)")
    e = sub.add_parser("enrich", help="pobierz szczegóły (VIN, zdjęcia, opis) kandydatów OtoMoto")
    e.add_argument("--limit", type=int, default=200)
    m = sub.add_parser("market", help="pobierz zdrowe auta tych modeli do wyceny")
    m.add_argument("--max-models", type=int, default=400)
    sub.add_parser("evaluate", help="wyceń i oceń kandydatów")
    sub.add_parser("alert", help="wyślij alerty Telegram")
    t = sub.add_parser("top", help="pokaż najlepszych kandydatów")
    t.add_argument("-n", type=int, default=20)
    sub.add_parser("run", help="pętla: wszystko według harmonogramu")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    conn = db.connect(cfg.database_url)
    db.init_db(conn)

    if args.cmd == "init-db":
        print("OK")
    elif args.cmd == "candidates":
        print(pipeline.scrape_candidates(cfg, conn, pipeline.make_http(cfg), full=args.full))
    elif args.cmd == "enrich":
        print(pipeline.enrich(cfg, conn, pipeline.make_http(cfg), args.limit))
    elif args.cmd == "market":
        print(pipeline.refresh_market(cfg, conn, pipeline.make_http(cfg), args.max_models))
        _mark(conn, "last_market")
    elif args.cmd == "evaluate":
        print(pipeline.evaluate_all(cfg, conn))
    elif args.cmd == "alert":
        print(pipeline.alert(cfg, conn))
    elif args.cmd == "top":
        _print_top(pipeline.top(conn, args.n))
    elif args.cmd == "run":
        run_loop(cfg, conn)


if __name__ == "__main__":
    main()
