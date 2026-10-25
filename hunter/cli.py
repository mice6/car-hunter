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
        print(f"{i:>2}. [id {r['id']}] {r['title'][:40]:<40} {r['year'] or '':>4} {r['power_hp'] or '':>4}KM "
              f"cena {fmt_pln(r['price_pln']):>11} | rynek {fmt_pln(r['market_value']):>11} ({r['comparables_n']}) "
              f"| naprawa {fmt_pln(r['repair_estimate']):>10} | oszcz. {fmt_pln(r['savings']):>11} "
              f"| ryzyko {float(r['risk'] or 0):.0%} | score {fmt_pln(r['score'])}")
        if flags:
            print(f"    ⚠ {flags}")
        print(f"    {r['url']}")


def _print_vision(conn, listing_id: int) -> None:
    import json
    r = conn.execute(
        """SELECT l.title, l.url, l.price_pln, l.vision, e.market_value, e.repair_estimate, e.savings, e.risk,
                  e.score, e.details FROM listings l LEFT JOIN evaluations e ON e.listing_id = l.id WHERE l.id = %s""",
        (listing_id,)).fetchone()
    if not r:
        print("Nie ma takiego ogłoszenia")
        return
    print(json.dumps(r["vision"], ensure_ascii=False, indent=2))
    dmg = (r["details"] or {}).get("damage") or {}
    print(f"\n{r['title']}  {r['url']}")
    print(f"cena {fmt_pln(r['price_pln'])} | rynek {fmt_pln(r['market_value'])} | naprawa {fmt_pln(r['repair_estimate'])}"
          f" | oszcz. {fmt_pln(r['savings'])} | ryzyko {float(r['risk'] or 0):.0%} | score {fmt_pln(r['score'])}")
    print("kosztorys:", json.dumps(dmg.get("breakdown"), ensure_ascii=False))
    print("flagi:", "; ".join(dmg.get("flags") or []))


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
    config_path = cfg.extra.get("path")
    while True:
        try:
            cfg = load_config(config_path)  # zmiany w config.yaml bez restartu
        except Exception:
            log.exception("config.yaml invalid, keeping previous config")
        sch = cfg.schedule
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
            # zdjęcia pakietami co N godzin: jeden zimny start endpointu zamiast 48 dziennie
            if cfg.vision_auto and _due(conn, "last_vision", cfg.vision.get("every_hours", 3)):
                res = pipeline.run_vision(cfg, conn)
                _mark(conn, "last_vision")
                if res.get("ok") or res.get("error"):
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
    vi = sub.add_parser("vision", help="oceń zdjęcia kandydatów modelem na RunPod")
    vi.add_argument("--limit", type=int)
    vi.add_argument("--id", type=int, help="tylko jedno ogłoszenie (id z bazy) i wypisz wynik")
    vi.add_argument("--dry", action="store_true", help="pokaż kolejkę, nic nie wysyłaj")
    vi.add_argument("--endpoint", help="inny endpoint RunPod (test A/B)")
    vi.add_argument("--model", help="nazwa modelu dla tego endpointu")
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
    elif args.cmd == "vision":
        if args.endpoint:
            cfg.runpod_endpoint_id = args.endpoint
        if args.model:
            cfg.data["vision"]["model"] = args.model
        res = pipeline.run_vision(cfg, conn, args.limit, args.id, dry=args.dry)
        if args.dry:
            for q in res.get("queue", []):
                print(f"[id {q['id']}] {q['title'][:45]:<45} cena {fmt_pln(q['price'])} | rynek {fmt_pln(q['market'])}"
                      f" | {q['photos']} zdjęć\n    {q['url']}")
            if not res.get("queue"):
                print("Kolejka pusta.")
        else:
            print(res)
        if not args.dry and (res.get("ok") or res.get("error")):
            pipeline.evaluate_all(cfg, conn)   # wyniki ze zdjęć od razu w rankingu
        if args.id is not None and not args.dry:
            _print_vision(conn, args.id)
    elif args.cmd == "alert":
        print(pipeline.alert(cfg, conn))
    elif args.cmd == "top":
        _print_top(pipeline.top(conn, args.n))
    elif args.cmd == "run":
        run_loop(cfg, conn)


if __name__ == "__main__":
    main()
