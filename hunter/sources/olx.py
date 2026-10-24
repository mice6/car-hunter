"""OLX: publiczne JSON API /api/v1/offers/ (zweryfikowane 2026-10-24).

- category_id=84 (samochody osobowe), marka = podkategoria (np. BMW=183, Kia=192),
  mapę slug->id daje /api/v1/friendly-links/query-params/motoryzacja,samochody,<slug>
- offset+limit maksymalnie do ~1000 wyników na zapytanie -> dzielimy na przedziały cen
- ogłoszenia z partner.code 'otomoto_*' / external_url to duplikaty OtoMoto -> pomijamy
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

from ..http import Http
from ..util import html_to_text, to_int

log = logging.getLogger(__name__)

BASE = "https://www.olx.pl"
API = BASE + "/api/v1/offers/"
SOURCE = "olx"
CARS_CATEGORY = 84
PAGE = 40
MAX_OFFSET = 1000
PRICE_BANDS = [(0, 15000), (15000, 25000), (25000, 35000), (35000, 50000), (50000, 80000),
               (80000, 120000), (120000, 200000), (200000, 10_000_000)]


def _param(o: dict, key: str, field: str = "key"):
    for p in o.get("params") or []:
        if p.get("key") == key:
            v = p.get("value") or {}
            return v.get(field) if field in v else v.get("label")
    return None


def is_otomoto_duplicate(o: dict) -> bool:
    code = ((o.get("partner") or {}).get("code") or "")
    return bool(o.get("external_url")) or code.startswith("otomoto")


def normalize(o: dict, makes: dict[int, str]) -> dict:
    price = next((p.get("value") or {} for p in o.get("params") or [] if p.get("key") == "price"), {})
    photos = []
    for ph in o.get("photos") or []:
        link = ph.get("link")
        if link:
            photos.append(link.replace("{width}", "1280").replace("{height}", "960"))
    loc = o.get("location") or {}
    cond = _param(o, "condition")
    rhd = _param(o, "righthanddrive")
    vin = _param(o, "vin", "label") or _param(o, "vin")
    return {
        "source": SOURCE,
        "source_id": str(o["id"]),
        "url": o.get("url"),
        "title": o.get("title"),
        "short_description": None,
        "make": makes.get(int((o.get("category") or {}).get("id") or 0)),
        "model": _param(o, "model"),
        "year": to_int(_param(o, "year")),
        "mileage_km": to_int(_param(o, "milage")),
        "power_hp": to_int(_param(o, "enginepower")),
        "engine_cc": to_int(_param(o, "enginesize")),
        "fuel": _param(o, "petrol"),
        "gearbox": _param(o, "transmission"),
        "body_type": _param(o, "car_body"),
        "drive": _param(o, "drive"),
        "price": to_int(price.get("value")),
        "currency": price.get("currency") or "PLN",
        "damaged": None if cond is None else cond == "damaged",
        "imported": None,
        "rhd": None if rhd is None else str(rhd).lower() in ("1", "yes", "tak", "right"),
        "vin": vin if vin and re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", str(vin).upper()) else None,
        "city": (loc.get("city") or {}).get("name"),
        "region": (loc.get("region") or {}).get("name"),
        "seller_type": "business" if o.get("business") else "private",
        "listed_at": o.get("created_time"),
        "description": html_to_text(o.get("description") or ""),
        "photos": photos,
        "raw": {"country_origin": _param(o, "country_origin")},
        "enriched": True,  # API zwraca pełne dane od razu
    }


def _query(http: Http, params: dict) -> tuple[int, list[dict]]:
    j = http.get(API, params=params, json=True,
                 headers={"Accept": "application/json", "Referer": BASE + "/motoryzacja/samochody/"})
    if not j:
        return 0, []
    meta = j.get("metadata") or {}
    return int(meta.get("visible_total_count") or meta.get("total_elements") or 0), j.get("data") or []


def search(http: Http, base_params: dict, makes: dict[int, str], price_from: int = 0,
           price_to: int = 10_000_000, stop_when_known=None) -> Iterator[dict]:
    bands = [(a, b) for a, b in PRICE_BANDS if b > price_from and a < price_to]
    for a, b in bands:
        lo, hi = max(a, price_from), min(b, price_to)
        offset = 0
        while offset < MAX_OFFSET:
            params = dict(base_params)
            params.update({"offset": offset, "limit": PAGE,
                           "filter_float_price:from": lo, "filter_float_price:to": hi})
            total, data = _query(http, params)
            log.info("olx %s-%s off %d: %d items (total %d)", lo, hi, offset, len(data), total)
            items = [normalize(o, makes) for o in data if not is_otomoto_duplicate(o)]
            stop = bool(stop_when_known and items and stop_when_known(items))  # przed zapisem
            yield from items
            if len(data) < PAGE or stop:
                break
            offset += PAGE


def candidate_params(crit: dict) -> dict:
    p = {
        "category_id": CARS_CATEGORY,
        "filter_enum_condition[0]": "damaged",
        "filter_enum_transmission[0]": crit.get("gearbox", "automatic"),
        "filter_float_enginepower:from": crit["min_power_hp"],
        "sort_by": "created_at:desc",
    }
    if crit.get("min_year"):
        p["filter_float_year:from"] = crit["min_year"]
    return p


def market_params(category_id: int, model: str, gearbox: str, year_from: int, year_to: int, min_power: int) -> dict:
    return {
        "category_id": category_id,
        "filter_enum_model[0]": model,
        "filter_enum_condition[0]": "notdamaged",
        "filter_enum_transmission[0]": gearbox,
        "filter_float_year:from": year_from,
        "filter_float_year:to": year_to,
        "filter_float_enginepower:from": min_power,
        "sort_by": "created_at:desc",
    }


def discover_makes(http: Http) -> dict[int, str]:
    """Jednorazowo: slugi marek ze strony kategorii -> category_id."""
    html = http.get(BASE + "/motoryzacja/samochody/") or ""
    slugs = sorted(set(re.findall(r'href="/motoryzacja/samochody/([a-z0-9-]+)/"', html)))
    out: dict[int, str] = {}
    for s in slugs:
        j = http.get(f"{BASE}/api/v1/friendly-links/query-params/motoryzacja,samochody,{s}", json=True)
        cid = ((j or {}).get("data") or {}).get("category_id")
        if cid and int(cid) != CARS_CATEGORY:
            out[int(cid)] = s
    log.info("olx: discovered %d makes", len(out))
    return out
