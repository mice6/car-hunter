"""OtoMoto: dane z osadzonego stanu Next.js (__NEXT_DATA__ -> urqlState -> advertSearch).

Struktura zweryfikowana 2026-10-24:
- lista: node.{id,title,createdAt,shortDescription,url,location,price.amount.{units,currencyCode},
  parameters[{key,value}], seller.__typename, thumbnail}
- szczegóły: pageProps.advert.{parametersDict{key:{values:[{value,label}]}}, images.photos[{url}],
  description (HTML), seller.type, updatedAt, status}
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterator

from ..http import Http
from ..util import html_to_text, to_int

log = logging.getLogger(__name__)

BASE = "https://www.otomoto.pl"
SOURCE = "otomoto"
PHOTO_SIZE = ";s=1280x960"

_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(html: str) -> dict:
    m = _NEXT_RE.search(html)
    if not m:
        raise ValueError("__NEXT_DATA__ not found (blokada lub zmiana strony)")
    return json.loads(m.group(1))


def search_params(filters: dict, page: int = 1) -> dict:
    """filters: {'filter_enum_damaged': '1', 'filter_float_engine_power:from': 170, 'order': '...'}"""
    params = {f"search[{k}]": str(v) for k, v in filters.items() if v is not None}
    if page > 1:
        params["page"] = str(page)
    return params


def parse_search(html: str) -> tuple[int, int, list[dict]]:
    """Zwraca (totalCount, pageSize, [znormalizowane ogłoszenia])."""
    state = next_data(html)["props"]["pageProps"].get("urqlState") or {}
    for entry in state.values():
        try:
            data = json.loads(entry.get("data") or "null")
        except (TypeError, ValueError):
            continue
        ads = (data or {}).get("advertSearch")
        if ads:
            page_size = (ads.get("pageInfo") or {}).get("pageSize") or 32
            items = [normalize_node(e["node"]) for e in ads.get("edges", []) if e.get("node")]
            return int(ads.get("totalCount") or 0), int(page_size), items
    return 0, 32, []


def normalize_node(node: dict) -> dict:
    p = {x.get("key"): x.get("value") for x in node.get("parameters") or []}
    amount = ((node.get("price") or {}).get("amount")) or {}
    loc = node.get("location") or {}
    seller = (node.get("seller") or {}).get("__typename", "")
    return {
        "source": SOURCE,
        "source_id": str(node["id"]),
        "url": node.get("url"),
        "title": node.get("title"),
        "short_description": node.get("shortDescription"),
        "make": p.get("make"),
        "model": p.get("model"),
        "year": to_int(p.get("year")),
        "mileage_km": to_int(p.get("mileage")),
        "power_hp": to_int(p.get("engine_power")),
        "engine_cc": to_int(p.get("engine_capacity")),
        "fuel": p.get("fuel_type"),
        "gearbox": p.get("gearbox"),
        "price": to_int(amount.get("units") or amount.get("value")),
        "currency": amount.get("currencyCode") or "PLN",
        "city": ((loc.get("city") or {}).get("name")),
        "region": ((loc.get("region") or {}).get("name")),
        "seller_type": "business" if "Professional" in seller or "Dealer" in seller else "private",
        "listed_at": node.get("createdAt"),
        "photos": [node["thumbnail"]["x1"]] if (node.get("thumbnail") or {}).get("x1") else [],
        "raw": {"priceEvaluation": (node.get("priceEvaluation") or {}).get("indicator"),
                "cepikVerified": node.get("cepikVerified")},
    }


def _pd(pd: dict, key: str):
    vals = (pd.get(key) or {}).get("values") or []
    return vals[0].get("value") if vals else None


def parse_cepik(pp: dict) -> dict | None:
    """Dane CEPiK z widgetu OtoMoto (tylko ogłoszenia z podanym nr rejestracyjnym)."""
    c = (((pp.get("cepikWidget") or {}).get("props") or {}).get("advert") or {}).get("cepik")
    if not c or not c.get("verifiedCar"):
        return None
    val = lambda k: (c.get(k) or {}).get("value")  # noqa: E731
    tl = c.get("timeline") or []
    inspections = [t for t in tl if t.get("type") == "periodic_technical_inspection"]
    failed = [t for t in inspections if ((t.get("details") or {}).get("result") or "").lower() not in ("positive", "")]
    abroad = next((t.get("date") for t in tl if t.get("type") == "first_registration_abroad"), None)
    in_pl = next((t.get("date") for t in tl if t.get("type") == "first_registration_in_poland"), None)
    owners = val("numberOfOwners")
    return {
        "verified": True,
        "owners": int(owners) if str(owners or "").isdigit() else None,
        "imported": val("importedCar"),
        "insurance_valid_until": val("insuranceValidity"),
        "inspection_valid_until": val("inspectionValidity"),
        "inspections": len(inspections),
        "failed_inspections": len(failed),
        "first_registration_abroad": abroad,
        "first_registration_pl": in_pl,
        "events": len(tl),
    }


def parse_detail(html: str) -> dict | None:
    pp = next_data(html)["props"]["pageProps"]
    ad = pp.get("advert")
    if not ad:
        return None
    pd = ad.get("parametersDict") or {}
    photos = []
    for ph in ((ad.get("images") or {}).get("photos") or []):
        url = ph.get("url")
        if url:
            photos.append(url if ";s=" in url else url + PHOTO_SIZE)
    flag = lambda k: (None if _pd(pd, k) is None else _pd(pd, k) == "1")  # noqa: E731
    equipment = [v.get("label") for g in ad.get("equipment") or [] for v in g.get("values") or [] if v.get("label")]
    return {
        "body_type": _pd(pd, "body_type"),
        "drive": _pd(pd, "transmission") or _pd(pd, "drive"),
        "vin": _pd(pd, "vin"),
        "damaged": flag("damaged"),
        "imported": flag("is_imported_car"),
        "has_registration": flag("has_registration"),
        "rhd": flag("rhd"),
        "description": html_to_text(ad.get("description") or ""),
        "photos": photos,
        "active": (ad.get("status") or "ACTIVE") == "ACTIVE",
        "equipment": equipment,
        "cepik": parse_cepik(pp),
    }


def search(http: Http, filters: dict, max_pages: int, path: str = "/osobowe",
           stop_when_known=None) -> Iterator[dict]:
    """Iteruje po wynikach. stop_when_known(items)->bool pozwala przerwać w trybie przyrostowym."""
    page = 1
    while page <= max_pages:
        html = http.get(BASE + path, params=search_params(filters, page))
        if not html:
            break
        total, page_size, items = parse_search(html)
        log.info("otomoto %s p%d: %d items (total %d)", path, page, len(items), total)
        # decyzję o stopie podejmujemy PRZED zapisem strony do bazy
        stop = bool(stop_when_known and items and stop_when_known(items))
        yield from items
        if not items or page * page_size >= total:
            break
        if stop:
            log.info("otomoto: incremental stop at page %d", page)
            break
        page += 1


def fetch_detail(http: Http, url: str) -> dict | None:
    html = http.get(url)
    if html is None:
        return {"active": False}
    return parse_detail(html)


def candidate_filters(crit: dict) -> dict:
    return {
        "filter_enum_damaged": "1",
        "filter_enum_gearbox": crit.get("gearbox"),
        "filter_float_engine_power:from": crit["min_power_hp"],
        "filter_float_price:to": crit["max_purchase_pln"],
        "filter_float_year:from": crit.get("min_year"),
        "filter_float_year:to": crit.get("max_year"),
        "filter_float_mileage:to": crit.get("max_mileage_km"),
        "order": "created_at_first:desc",
    }


def market_filters(gearbox: str, year_from: int, year_to: int, min_power: int) -> dict:
    return {
        "filter_enum_damaged": "0",
        "filter_enum_gearbox": gearbox,
        "filter_float_year:from": year_from,
        "filter_float_year:to": year_to,
        "filter_float_engine_power:from": min_power,
        "order": "created_at_first:desc",
    }
