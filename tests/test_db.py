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


def test_vision_prefilter_and_save(conn, monkeypatch):
    from hunter import vision as vmod
    rnd = random.Random(3)
    for i in range(30):
        y = rnd.randint(2016, 2020)
        db.upsert_listing(conn, _item(2000 + i, year=y, price=int(80000 * 1.1 ** (y - 2016))), "market", 4.25)
    good, _ = db.upsert_listing(conn, _item(20, price=25000), "candidate", 4.25)
    db.apply_detail(conn, good, {"photos": [f"p{i}/image;s=1280x960" for i in range(8)], "active": True})
    few, _ = db.upsert_listing(conn, _item(21, price=25000), "candidate", 4.25)   # 1 miniatura, nie wzbogacony
    pricey, _ = db.upsert_listing(conn, _item(22, price=52000), "candidate", 4.25)
    db.apply_detail(conn, pricey, {"photos": ["x"] * 8, "active": True})
    cfg = load_config("/nonexistent.yaml")
    cfg.runpod_api_key, cfg.runpod_endpoint_id = "k", "e"
    pipeline.evaluate_all(cfg, conn)

    seen = []

    def fake_analyze(vcfg, lst):
        seen.append(lst["id"])
        return {"parts": [], "damage_zones": [], "photos_cover": {}, "overall_severity": "light",
                "confidence": 0.9, "notes_pl": "ok"}

    monkeypatch.setattr(vmod, "analyze", fake_analyze)
    fetched = []

    def fake_detail(http, url):   # nie wzbogacone ogłoszenie z kolejki: dociągnięte, ale ma 2 zdjęcia -> pominięte
        fetched.append(url)
        return {"photos": ["a/image", "b/image"], "description": "ok", "active": True}

    monkeypatch.setattr(pipeline.otomoto, "fetch_detail", fake_detail)
    assert pipeline.run_vision(cfg, conn)["ok"] == 1
    assert seen == [good]
    assert fetched == ["https://www.otomoto.pl/oferta/21"]
    row = conn.execute("SELECT vision, vision_at FROM listings WHERE id=%s", (good,)).fetchone()
    assert row["vision"]["notes_pl"] == "ok" and row["vision_at"]
    assert pipeline.run_vision(cfg, conn) == {"queued": 0}   # nie powtarza
    pipeline.evaluate_all(cfg, conn)
    src = conn.execute("SELECT details->'damage'->>'source' AS s FROM evaluations WHERE listing_id=%s", (good,)).fetchone()
    assert src["s"] == "vision"
    assert few != good and pricey != good
    q = pipeline.run_vision(cfg, conn, dry=True)
    assert q == {"queue": []}


def test_vision_queue_skips_cars_that_cannot_fit_budget(conn):
    for i in range(30):
        db.upsert_listing(conn, _item(3000 + i, model="a", year=2018, mileage_km=90000, price=120000), "market", 4.25)
        db.upsert_listing(conn, _item(4000 + i, model="b", year=2018, mileage_km=90000, price=70000), "market", 4.25)
    # 45k przy rynku 120k: min. naprawa 0.25*75k=18.75k -> 66k > 55k (stara reguła 2.5k by go przepuściła)
    pricey, _ = db.upsert_listing(conn, _item(30, model="a", year=2018, mileage_km=90000, price=45000), "candidate", 4.25)
    # 20k przy rynku 70k: 20k + ~2k + 12.5k = ~35k -> mieści się
    fits, _ = db.upsert_listing(conn, _item(31, model="b", year=2018, mileage_km=90000, price=20000), "candidate", 4.25)
    for lid in (pricey, fits):
        db.apply_detail(conn, lid, {"photos": ["x"] * 8, "active": True})
    cfg = load_config("/nonexistent.yaml")
    pipeline.evaluate_all(cfg, conn)
    ids = [q["id"] for q in pipeline.run_vision(cfg, conn, dry=True)["queue"]]
    assert ids == [fits]
