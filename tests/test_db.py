"""Test integracyjny warstwy bazy. Uruchamiany tylko z HUNTER_TEST_DB=postgresql://..."""
import os
import random

import pytest

from hunter import db, pipeline
from hunter.config import load_config

DSN = os.environ.get("HUNTER_TEST_DB")
pytestmark = pytest.mark.skipif(not DSN, reason="HUNTER_TEST_DB not set")


@pytest.fixture()
def conn():
    c = db.connect(DSN)
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init_db(c)
    yield c
    c.close()


def _item(i, **kw):
    base = {"source": "otomoto", "source_id": str(i), "url": f"https://www.otomoto.pl/oferta/{i}",
            "title": "Kia Stinger", "make": "kia", "model": "stinger", "year": 2018, "mileage_km": 120000,
            "power_hp": 200, "fuel": "diesel", "gearbox": "automatic", "price": 30000, "currency": "PLN",
            "photos": ["thumb"], "raw": {}}
    base.update(kw)
    return base


def test_upsert_price_history_and_kind_priority(conn):
    lid, new = db.upsert_listing(conn, _item(1), "candidate", 4.25)
    assert new
    _, new2 = db.upsert_listing(conn, _item(1, price=28000, title=None), "market", 4.25)
    assert not new2
    row = conn.execute("SELECT * FROM listings WHERE id=%s", (lid,)).fetchone()
    assert row["kind"] == "candidate" and float(row["price_pln"]) == 28000 and row["title"] == "Kia Stinger"
    hist = conn.execute("SELECT price_pln FROM price_history WHERE listing_id=%s ORDER BY seen_at", (lid,)).fetchall()
    assert [float(h["price_pln"]) for h in hist] == [30000, 28000]
    eur, _ = db.upsert_listing(conn, _item(2, price=10000, currency="EUR"), "candidate", 4.25)
    assert float(conn.execute("SELECT price_pln FROM listings WHERE id=%s", (eur,)).fetchone()["price_pln"]) == 42500


def test_detail_and_enriched_insert(conn):
    lid, _ = db.upsert_listing(conn, _item(3), "candidate", 4.25)
    db.apply_detail(conn, lid, {"body_type": "combi", "vin": "KNAE251LDJ6012345", "has_registration": True,
                                "description": "Lekko obtarty", "photos": ["a", "b", "c"], "active": True,
                                "equipment": ["Kamera"]})
    row = conn.execute("SELECT * FROM listings WHERE id=%s", (lid,)).fetchone()
    assert row["body_type"] == "combi" and len(row["photos"]) == 3 and row["enriched_at"]
    assert row["raw"]["equipment"] == ["Kamera"]
    # odświeżenie z listy (1 miniatura) nie kasuje pełnych zdjęć ani opisu
    db.upsert_listing(conn, _item(3), "candidate", 4.25)
    row = conn.execute("SELECT * FROM listings WHERE id=%s", (lid,)).fetchone()
    assert len(row["photos"]) == 3 and row["description"] == "Lekko obtarty"
    oid, _ = db.upsert_listing(conn, _item(4, source="olx", enriched=True, body_type="estate-car"), "candidate", 4.25)
    row = conn.execute("SELECT * FROM listings WHERE id=%s", (oid,)).fetchone()
    assert row["enriched_at"] is not None and row["body_type"] == "combi"


def test_evaluate_all_and_top(conn):
    rnd = random.Random(7)
    for i in range(30):
        y = rnd.randint(2016, 2020)
        km = rnd.randint(60000, 200000)
        db.upsert_listing(conn, _item(1000 + i, year=y, mileage_km=km,
                                      price=int(80000 * 1.1 ** (y - 2016) * (1 - km / 500000))), "market", 4.25)
    good, _ = db.upsert_listing(conn, _item(10, price=25000, description="lekko obtarty zderzak"), "candidate", 4.25)
    db.apply_detail(conn, good, {"body_type": "combi", "description": "lekko obtarty zderzak", "photos": ["x"] * 10,
                                 "active": True})
    db.upsert_listing(conn, _item(11, price=49000), "candidate", 4.25)
    cfg = load_config("/nonexistent.yaml")
    stats = pipeline.evaluate_all(cfg, conn)
    assert stats["passing"] >= 1
    rows = pipeline.top(conn, 5)
    assert rows[0]["id"] == good and float(rows[0]["savings"]) > 0
    assert all(r["id"] != 0 for r in rows)
