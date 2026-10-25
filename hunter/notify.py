from __future__ import annotations

import html
import logging

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
    lines = [
        f"🚗 <b>{html.escape(row.get('title') or '')}</b>",
        html.escape(spec),
        f"Cena: <b>{fmt_pln(row.get('price_pln'))}</b> · {html.escape(row.get('city') or '')}",
        f"Wycena zdrowego: {fmt_pln(row.get('market_value'))} ({row.get('comparables_n')} por.)",
        f"Naprawa ({'ze zdjęć' if dmg.get('source') == 'vision' else 'szac. z opisu'}, {dmg.get('severity', '?')}): {fmt_pln(row.get('repair_estimate'))} · opłaty {fmt_pln(row.get('fees'))}",
        f"Oszczędność: <b>{fmt_pln(row.get('savings'))}</b> · ryzyko {float(row.get('risk') or 0):.0%} · score {fmt_pln(row.get('score'))}",
    ]
    if flags:
        lines.append("⚠️ " + html.escape("; ".join(flags)))
    if dmg.get("notes"):
        lines.append("🔍 " + html.escape(dmg["notes"]))
    lines.append(html.escape(row.get("url") or ""))
    return "\n".join(lines)


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
