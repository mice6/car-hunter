from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .util import norm_body, norm_fuel

SCHEMA = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

LISTING_FIELDS = [
    "url", "title", "short_description", "make", "model", "year", "mileage_km", "power_hp", "engine_cc",
    "fuel", "gearbox", "body_type", "drive", "price_pln", "damaged", "imported", "has_registration", "rhd",
    "vin", "city", "region", "seller_type", "description", "photos", "raw", "listed_at",
]


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row, autocommit=True)


def init_db(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text(encoding="utf-8"))


def to_pln(item: dict, eur_pln: float) -> float | None:
    price = item.get("price")
    if price is None:
        return None
    cur = (item.get("currency") or "PLN").upper()
    return float(price) * eur_pln if cur == "EUR" else float(price)


def known_ids(conn, source: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute("SELECT source_id FROM listings WHERE source=%s AND source_id = ANY(%s)", (source, ids)).fetchall()
    return {r["source_id"] for r in rows}


def upsert_listing(conn, item: dict, kind: str, eur_pln: float) -> tuple[int, bool]:
    """Zapis/aktualizacja ogłoszenia. Zwraca (id, czy_nowe). Śledzi zmiany ceny."""
    rec = dict(item)
    rec["price_pln"] = to_pln(item, eur_pln)
    rec["fuel"] = norm_fuel(rec.get("fuel"))
    rec["body_type"] = norm_body(rec.get("body_type"))
    rec["photos"] = json.dumps(rec.get("photos") or [])
    rec["raw"] = json.dumps(rec.get("raw") or {})
    vals = {f: rec.get(f) for f in LISTING_FIELDS}

    old = conn.execute("SELECT id, price_pln, kind FROM listings WHERE source=%s AND source_id=%s",
                       (item["source"], item["source_id"])).fetchone()
    if old is None:
        cols = ["source", "source_id", "kind"] + LISTING_FIELDS
        if item.get("enriched"):
            cols.append("enriched_at")
        params = [item["source"], item["source_id"], kind] + [vals[f] for f in LISTING_FIELDS]
        sql_vals = ", ".join(["%s"] * (len(cols) - (1 if item.get("enriched") else 0)))
        if item.get("enriched"):
            sql_vals += ", now()"
        row = conn.execute(f"INSERT INTO listings ({', '.join(cols)}) VALUES ({sql_vals}) RETURNING id",
                           params).fetchone()
        if vals["price_pln"] is not None:
            conn.execute("INSERT INTO price_history (listing_id, price_pln) VALUES (%s, %s)", (row["id"], vals["price_pln"]))
        return row["id"], True

    # aktualizacja: nie nadpisujemy wartości wzbogaconych (np. opis, VIN) pustymi z listy
    sets = ", ".join(f"{f} = COALESCE(%s, {f})" for f in LISTING_FIELDS if f not in ("photos", "raw"))
    params = [vals[f] for f in LISTING_FIELDS if f not in ("photos", "raw")]
    # zdjęcia: bierzemy nowe tylko, gdy jest ich więcej (lista ma tylko miniaturę)
    sets += ", photos = CASE WHEN jsonb_array_length(%s::jsonb) > jsonb_array_length(photos) THEN %s::jsonb ELSE photos END"
    params += [vals["photos"], vals["photos"]]
    # kandydat ma pierwszeństwo przed rynkiem (to samo auto może trafić w oba zapytania)
    new_kind = "candidate" if "candidate" in (kind, old["kind"]) else kind
    sets += ", kind = %s, last_seen = now(), active = true"
    params.append(new_kind)
    if item.get("enriched"):
        sets += ", enriched_at = now()"
    conn.execute(f"UPDATE listings SET {sets} WHERE id = %s", params + [old["id"]])
    if vals["price_pln"] is not None and (old["price_pln"] is None or float(old["price_pln"]) != float(vals["price_pln"])):
        conn.execute("INSERT INTO price_history (listing_id, price_pln) VALUES (%s, %s)", (old["id"], vals["price_pln"]))
    return old["id"], False


def apply_detail(conn, listing_id: int, d: dict) -> None:
    conn.execute(
        """UPDATE listings SET
             body_type = COALESCE(%(body_type)s, body_type),
             drive = COALESCE(%(drive)s, drive),
             vin = COALESCE(%(vin)s, vin),
             damaged = COALESCE(%(damaged)s, damaged),
             imported = COALESCE(%(imported)s, imported),
             has_registration = COALESCE(%(has_registration)s, has_registration),
             -- CEPiK z ważnym OC = auto zarejestrowane w PL
             rhd = COALESCE(%(rhd)s, rhd),
             description = COALESCE(NULLIF(%(description)s, ''), description),
             photos = CASE WHEN jsonb_array_length(%(photos)s::jsonb) > 0 THEN %(photos)s::jsonb ELSE photos END,
             raw = raw || %(extra)s::jsonb,
             active = %(active)s,
             enriched_at = now()
           WHERE id = %(id)s""",
        {
            "id": listing_id,
            "body_type": norm_body(d.get("body_type")),
            "drive": d.get("drive"),
            "vin": d.get("vin"),
            "damaged": d.get("damaged"),
            "imported": d.get("imported"),
            "has_registration": d.get("has_registration"),
            "rhd": d.get("rhd"),
            "description": d.get("description") or "",
            "photos": json.dumps(d.get("photos") or []),
            "extra": json.dumps({"equipment": d.get("equipment") or [], "cepik": d.get("cepik")}),
            "active": d.get("active", True),
        },
    )


def deactivate_stale(conn, kind: str, hours: int) -> int:
    cur = conn.execute(
        "UPDATE listings SET active=false WHERE kind=%s AND active AND last_seen < now() - make_interval(hours => %s)",
        (kind, hours))
    return cur.rowcount


def kv_get(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM kv WHERE key=%s", (key,)).fetchone()
    return r["value"] if r else None


def kv_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                 (key, value))
