from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from . import db
from .config import Config
from .http import Http
from .notify import format_alert, send_telegram
from .scoring import evaluate, value_from_comparables
from .sources import olx, otomoto

log = logging.getLogger(__name__)


def make_http(cfg: Config) -> Http:
    s = cfg.scraper
    return Http(s["delay_min_s"], s["delay_max_s"])


# ------------------------------------------------------------------ OLX: mapa marek

def olx_makes(conn, http: Http) -> dict[int, str]:
    rows = conn.execute("SELECT category_id, slug FROM olx_makes").fetchall()
    if rows:
        return {r["category_id"]: r["slug"] for r in rows}
    makes = olx.discover_makes(http)
    for cid, s in makes.items():
        conn.execute("INSERT INTO olx_makes (category_id, slug) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cid, s))
    return makes


# ------------------------------------------------------------------ kandydaci (uszkodzone)

def _incremental_stopper(conn, source: str, pages_needed: int):
    state = {"known_pages": 0}

    def stop(items: list[dict]) -> bool:
        ids = [i["source_id"] for i in items]
        if len(db.known_ids(conn, source, ids)) == len(ids):
            state["known_pages"] += 1
        else:
            state["known_pages"] = 0
        return state["known_pages"] >= pages_needed

    return stop


def scrape_candidates(cfg: Config, conn, http: Http, full: bool = False) -> dict:
    crit, s = cfg.criteria, cfg.scraper
    stats = defaultdict(int)
    eur = s["eur_pln"]

    if "otomoto" in s["sources"]:
        stopper = None if full else _incremental_stopper(conn, "otomoto", s["incremental_known_pages_stop"])
        # stopper musi widzieć stan PRZED zapisem strony -> sprawdzamy w generatorze przed upsertem
        for item in otomoto.search(http, otomoto.candidate_filters(crit), s["candidate_max_pages"],
                                   stop_when_known=stopper):
            _, new = db.upsert_listing(conn, item, "candidate", eur)
            stats["otomoto_new" if new else "otomoto_seen"] += 1

    if "olx" in s["sources"]:
        makes = olx_makes(conn, http)
        stopper = None if full else _incremental_stopper(conn, "olx", 1)
        for item in olx.search(http, olx.candidate_params(crit), makes, 0, crit["max_purchase_pln"],
                               stop_when_known=stopper):
            if (item.get("power_hp") or 0) < crit["min_power_hp"]:
                continue
            _, new = db.upsert_listing(conn, item, "candidate", eur)
            stats["olx_new" if new else "olx_seen"] += 1

    if full:
        stats["deactivated"] = db.deactivate_stale(conn, "candidate", 26)
        db.kv_set(conn, "last_full_sweep", datetime.now(timezone.utc).isoformat())
    log.info("candidates: %s", dict(stats))
    return dict(stats)


# ------------------------------------------------------------------ szczegóły ogłoszeń OtoMoto

def enrich(cfg: Config, conn, http: Http, limit: int = 200) -> int:
    rows = conn.execute(
        """SELECT id, url FROM listings
           WHERE kind='candidate' AND active AND source='otomoto'
             AND (enriched_at IS NULL OR enriched_at < now() - interval '3 days')
             AND price_pln <= %s
           ORDER BY enriched_at NULLS FIRST, first_seen DESC LIMIT %s""",
        (cfg.criteria["max_purchase_pln"], limit)).fetchall()
    done = 0
    for r in rows:
        try:
            d = otomoto.fetch_detail(http, r["url"])
        except Exception as e:
            log.warning("detail %s failed: %s", r["url"], e)
            continue
        if d is None:
            continue
        db.apply_detail(conn, r["id"], d)
        done += 1
    log.info("enriched %d/%d", done, len(rows))
    return done


# ------------------------------------------------------------------ rynek zdrowych aut (wycena)

def refresh_market(cfg: Config, conn, http: Http, max_models: int = 400) -> dict:
    s, crit = cfg.scraper, cfg.criteria
    span = s["market_year_span"]
    models = conn.execute(
        """SELECT l.make, l.model, min(l.year) AS ymin, max(l.year) AS ymax, count(*) AS n
           FROM listings l
           LEFT JOIN market_refresh m ON m.source='otomoto' AND m.make=l.make AND m.model=l.model
           WHERE l.kind='candidate' AND l.active AND l.make IS NOT NULL AND l.model IS NOT NULL
             AND l.price_pln <= %s
             AND (m.refreshed_at IS NULL OR m.refreshed_at < now() - make_interval(hours => %s))
           GROUP BY l.make, l.model
           ORDER BY count(*) DESC
           LIMIT %s""",
        (crit["max_purchase_pln"], s["market_refresh_hours"], max_models)).fetchall()
    stats = defaultdict(int)
    makes_by_slug = {v: k for k, v in olx_makes(conn, http).items()} if "olx" in s["sources"] else {}
    for m in models:
        y_from = (m["ymin"] or 2010) - span
        y_to = (m["ymax"] or 2026) + span
        found = 0
        flt = otomoto.market_filters(crit.get("gearbox", "automatic"), y_from, y_to, s["market_min_power_hp"])
        try:
            for item in otomoto.search(http, flt, s["market_max_pages_per_model"], path=f"/osobowe/{m['make']}/{m['model']}"):
                db.upsert_listing(conn, item, "market", s["eur_pln"])
                found += 1
            cid = makes_by_slug.get(m["make"])
            if cid:
                p = olx.market_params(cid, m["model"], crit.get("gearbox", "automatic"), y_from, y_to, s["market_min_power_hp"])
                for item in olx.search(http, p, {cid: m["make"]}):
                    db.upsert_listing(conn, item, "market", s["eur_pln"])
                    found += 1
        except Exception as e:
            log.warning("market %s %s failed: %s", m["make"], m["model"], e)
            continue
        conn.execute(
            """INSERT INTO market_refresh (source, make, model, refreshed_at, found) VALUES ('otomoto', %s, %s, now(), %s)
               ON CONFLICT (source, make, model) DO UPDATE SET refreshed_at=now(), found=EXCLUDED.found""",
            (m["make"], m["model"], found))
        stats["models"] += 1
        stats["listings"] += found
        log.info("market %s %s (%s-%s): %d", m["make"], m["model"], y_from, y_to, found)
    return dict(stats)


# ------------------------------------------------------------------ ocena

def evaluate_all(cfg: Config, conn) -> dict:
    cands = conn.execute(
        """SELECT * FROM listings WHERE kind='candidate' AND active AND price_pln IS NOT NULL""").fetchall()
    comps_cache: dict[tuple, list[dict]] = {}
    stats = defaultdict(int)
    for c in cands:
        key = (c["make"], c["model"], c["gearbox"])
        if key not in comps_cache:
            comps_cache[key] = conn.execute(
                """SELECT year, mileage_km, power_hp, fuel, price_pln FROM listings
                   WHERE kind='market' AND make=%s AND model=%s AND gearbox IS NOT DISTINCT FROM %s
                     AND last_seen > now() - interval '60 days' AND price_pln > 3000""",
                key).fetchall()
        val = value_from_comparables(c, comps_cache[key], cfg.scoring["min_comparables"])
        ev = evaluate(c, val, cfg.criteria, cfg.scoring)
        conn.execute(
            """INSERT INTO evaluations (listing_id, market_value, valuation_method, comparables_n, repair_estimate,
                   fees, total_cost, savings, risk, score, passes_filters, details, evaluated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (listing_id) DO UPDATE SET market_value=EXCLUDED.market_value,
                   valuation_method=EXCLUDED.valuation_method, comparables_n=EXCLUDED.comparables_n,
                   repair_estimate=EXCLUDED.repair_estimate, fees=EXCLUDED.fees, total_cost=EXCLUDED.total_cost,
                   savings=EXCLUDED.savings, risk=EXCLUDED.risk, score=EXCLUDED.score,
                   passes_filters=EXCLUDED.passes_filters, details=EXCLUDED.details, evaluated_at=now()""",
            (c["id"], ev.market_value, ev.valuation_method, ev.comparables_n, ev.repair_estimate, ev.fees,
             ev.total_cost, ev.savings, ev.risk, ev.score, ev.passes_filters, json.dumps(ev.details, ensure_ascii=False)))
        stats["passing" if ev.passes_filters else "rejected"] += 1
        if ev.market_value is None:
            stats["no_valuation"] += 1
    log.info("evaluated: %s", dict(stats))
    return dict(stats)


TOP_SQL = """
SELECT l.id, l.source, l.title, l.url, l.year, l.mileage_km, l.power_hp, l.fuel, l.body_type, l.city,
       l.price_pln, e.market_value, e.comparables_n, e.repair_estimate, e.fees, e.total_cost, e.savings,
       e.risk, e.score, e.details, e.alerted_at
FROM evaluations e JOIN listings l ON l.id = e.listing_id
WHERE l.active AND e.passes_filters {extra}
ORDER BY e.score DESC
LIMIT %s
"""


def alert(cfg: Config, conn, max_alerts: int = 10) -> int:
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        log.info("telegram not configured, skipping alerts")
        return 0
    rows = conn.execute(
        TOP_SQL.format(extra="AND e.alerted_at IS NULL AND e.score >= %s"),
        (cfg.scoring["min_score_alert_pln"], max_alerts)).fetchall()
    sent = 0
    for r in rows:
        if send_telegram(cfg.telegram_token, cfg.telegram_chat_id, format_alert(r)):
            conn.execute("UPDATE evaluations SET alerted_at = now() WHERE listing_id=%s", (r["id"],))
            sent += 1
    log.info("alerts sent: %d", sent)
    return sent


def top(conn, n: int = 20) -> list[dict]:
    return conn.execute(TOP_SQL.format(extra=""), (n,)).fetchall()
