from __future__ import annotations

import html
import logging
from urllib.parse import quote_plus

from curl_cffi import requests as creq

log = logging.getLogger(__name__)


def fmt_pln(v) -> str:
    return "—" if v is None else f"{float(v):,.0f} zł".replace(",", " ")


def format_alert(row: dict) -> str:
    d = row.get("details") or {}
    dmg = d.get("damage") or {}
    flags = dmg.get("flags") or []
    spec = " · ".join(str(x) for x in [
        row.get("year"), f"{row['mileage_km']:,} km".replace(",", " ") if row.get("mileage_km") else None,
        f"{row['power_hp']} KM" if row.get("power_hp") else None, row.get("fuel"), row.get("body_type"),
    ] if x)
    gem = d.get("gem")
    lines = [
        f"{'💎 <b>PEREŁKA</b> · ' if gem else '🚗 '}<b>{html.escape(row.get('title') or '')}</b>",
        html.escape(spec),
        f"Cena: <b>{fmt_pln(row.get('price_pln'))}</b> · {html.escape(row.get('city') or '')}",
        f"Wycena zdrowego: {fmt_pln(row.get('market_value'))} ({row.get('comparables_n')} por.)",
        f"Cena to {float(d.get('price_ratio') or 0):.0%} wartości zdrowego",
        f"Naprawa ({'ze zdjęć' if dmg.get('source') == 'vision' else 'szac. z opisu'}, {dmg.get('severity', '?')}): {fmt_pln(row.get('repair_estimate'))} · opłaty {fmt_pln(row.get('fees'))}",
        f"Oszczędność: <b>{fmt_pln(row.get('savings'))}</b> · ryzyko {float(row.get('risk') or 0):.0%} · score {fmt_pln(row.get('score'))}",
    ]
    if flags:
        lines.append("⚠️ " + html.escape("; ".join(flags)))
    if dmg.get("notes"):
        lines.append("🔍 " + html.escape(dmg["notes"]))
    cep = row.get("cepik")
    if cep:
        lines.append("📋 CEPiK: " + html.escape(cepik_summary(cep)))
    if row.get("vin"):
        q = quote_plus(f'"{row["vin"]}"')
        lines.append(f'VIN {html.escape(row["vin"])}: <a href="https://www.google.com/search?q={q}">historia aukcji/zdjęcia</a>'
                     ' · <a href="https://historiapojazdu.gov.pl/">historiapojazdu.gov.pl</a>')
    lines.append(html.escape(row.get("url") or ""))
    return "\n".join(lines)


def cepik_summary(c: dict) -> str:
    parts = []
    if c.get("owners") is not None:
        parts.append(f"{c['owners']} właść.")
    if c.get("inspections"):
        bad = c.get("failed_inspections") or 0
        parts.append(f"przeglądy {c['inspections']}" + (f" ({bad} negat.)" if bad else " ok"))
    if c.get("insurance_valid_until"):
        parts.append(f"OC do {c['insurance_valid_until']}")
    if c.get("first_registration_abroad"):
        parts.append(f"1. rej. za granicą {c['first_registration_abroad']}")
    if c.get("first_registration_pl"):
        parts.append(f"w PL od {c['first_registration_pl']}")
    return " · ".join(parts) or "zweryfikowany"


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        r = creq.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": False}, timeout=20)
        if r.status_code != 200:
            log.error("telegram %d: %s", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        log.error("telegram error: %s", e)
        return False
