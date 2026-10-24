import random

from hunter.config import load_config
from hunter.scoring import Valuation, assess_damage, evaluate, value_from_comparables

CFG = load_config("/nonexistent.yaml")


def _market(n=40, seed=1):
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        year = rnd.randint(2016, 2021)
        km = rnd.randint(40_000, 220_000)
        price = 60000 * (1.12 ** (year - 2016)) * (1 - km / 600_000) * rnd.uniform(0.93, 1.07)
        out.append({"year": year, "mileage_km": km, "power_hp": 200, "fuel": "diesel", "price_pln": price})
    return out


def test_regression_valuation_reasonable():
    target = {"year": 2018, "mileage_km": 130_000, "power_hp": 200, "fuel": "diesel"}
    v = value_from_comparables(target, _market())
    expected = 60000 * 1.12 ** 2 * (1 - 130_000 / 600_000)
    assert v.value and abs(v.value - expected) / expected < 0.10
    assert v.method.startswith("regression")


def test_small_sample_falls_back_to_median():
    target = {"year": 2018, "mileage_km": 130_000, "power_hp": 200, "fuel": "diesel"}
    v = value_from_comparables(target, _market(6))
    assert v.method.startswith("nearest_median") and v.n == 6


def test_too_few():
    v = value_from_comparables({"year": 2018}, _market(3))
    assert v.value is None


def test_damage_keywords():
    assert assess_damage({"title": "Chrysler 300C", "description": "panorama dach, relingi dachowe"}).severity == "medium"
    assert assess_damage({"title": "BMW", "description": "Wystrzelone poduszki, krzywa podłużnica"}).severity == "severe"
    light = assess_damage({"title": "Audi", "description": "Lekko obtarty zderzak tył, rysa na drzwiach"})
    assert light.severity == "light"
    usa = assess_damage({"title": "Kia Stinger", "description": "Jedzie z USA, Copart"})
    assert "import z USA" in usa.flags and usa.risk > 0.2


def test_evaluate_pass_and_budget():
    listing = {"price_pln": 30000, "power_hp": 200, "gearbox": "automatic", "fuel": "diesel",
               "body_type": "combi", "title": "Skoda Superb", "description": "lekko obtarty zderzak"}
    ev = evaluate(listing, Valuation(60000, "test", 20), CFG.criteria, CFG.scoring)
    assert ev.passes_filters, ev.details
    assert ev.savings > 15000 and ev.score > ev.savings * 0.8

    expensive = dict(listing, price_pln=50000)
    ev2 = evaluate(expensive, Valuation(80000, "test", 20), CFG.criteria, CFG.scoring)
    assert not ev2.passes_filters  # 50k + naprawa + opłaty > 55k


def test_evaluate_rejects_weak_engine():
    listing = {"price_pln": 20000, "power_hp": 150, "gearbox": "automatic", "title": "x"}
    ev = evaluate(listing, Valuation(60000, "test", 20), CFG.criteria, CFG.scoring)
    assert not ev.passes_filters and "za słaby silnik" in ev.details["reasons"]
