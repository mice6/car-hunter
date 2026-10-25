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
    # bez zdjęć połowa różnicy cena-rynek liczona jako szkoda: 0.5 * 30 000
    assert ev.repair_estimate == 15000 and ev.details["repair_basis"] == "implied"
    assert ev.savings > 5000 and ev.score > ev.savings * 0.8

    expensive = dict(listing, price_pln=50000)
    ev2 = evaluate(expensive, Valuation(80000, "test", 20), CFG.criteria, CFG.scoring)
    assert not ev2.passes_filters  # 50k + naprawa + opłaty > 55k


def test_evaluate_rejects_weak_engine():
    listing = {"price_pln": 20000, "power_hp": 150, "gearbox": "automatic", "title": "x"}
    ev = evaluate(listing, Valuation(60000, "test", 20), CFG.criteria, CFG.scoring)
    assert not ev.passes_filters and "za słaby silnik" in ev.details["reasons"]


def test_criteria_filters():
    from hunter.scoring import criteria_failures
    base = {"price_pln": 30000, "power_hp": 200, "gearbox": "automatic", "make": "kia", "model": "stinger",
            "fuel": "petrol", "body_type": "sedan", "year": 2018, "mileage_km": 150000, "region": "Dolnośląskie"}
    crit = dict(CFG.criteria)
    assert criteria_failures(base, crit) == []
    assert "za duży przebieg" in criteria_failures(base, dict(crit, max_mileage_km=100000))
    assert "paliwo petrol" in criteria_failures(base, dict(crit, fuels=["diesel"]))
    assert "nadwozie sedan" in criteria_failures(base, dict(crit, bodies=["suv", "combi"]))
    assert criteria_failures(base, dict(crit, include_models=["Kia/Stinger", "bmw/seria-5"])) == []
    assert "model spoza listy" in criteria_failures(base, dict(crit, include_models=["bmw/seria-5"]))
    assert "marka wykluczona" in criteria_failures(base, dict(crit, exclude_makes=["kia"]))
    assert criteria_failures(base, dict(crit, regions=["dolnośląskie"])) == []
    assert "import z USA" in criteria_failures(dict(base, description="Copart"), dict(crit, exclude_us_imports=True))


def test_search_targets():
    from hunter.pipeline import search_targets
    assert search_targets({}) == [(None, None)]
    assert search_targets({"include_makes": ["Kia", "bmw"]}) == [("kia", None), ("bmw", None)]
    assert search_targets({"include_makes": ["kia"], "include_models": ["volvo/xc60"]}) == [("volvo", "xc60")]


def test_make_preference_bonus():
    listing = {"price_pln": 30000, "power_hp": 200, "gearbox": "automatic", "make": "volvo", "title": "x",
               "description": "lekko obtarty"}
    a = evaluate(listing, Valuation(60000, "t", 20), CFG.criteria, CFG.scoring, {"makes": []})
    b = evaluate(listing, Valuation(60000, "t", 20), CFG.criteria, CFG.scoring, {"makes": ["volvo"]})
    assert b.score - a.score == CFG.scoring["pref_bonus_pln"]


def test_traps_and_price_ratio():
    base = {"price_pln": 30000, "power_hp": 250, "gearbox": "automatic", "title": "BMW X5", "description": ""}
    v = Valuation(150000, "t", 30)
    cheap = evaluate(dict(base, price_pln=9000), v, CFG.criteria, CFG.scoring)
    assert not cheap.passes_filters and any("podejrzanie niska" in r for r in cheap.details["reasons"])
    assert not cheap.details["criteria_ok"]
    for desc, reason in [("Sprzedam w całości na części", "na części"), ("Cesja leasingu, odstępne 20 tys", "cesja"),
                         ("Auto bez dokumentów", "bez dokumentów")]:
        ev = evaluate(dict(base, description=desc), v, CFG.criteria, CFG.scoring)
        assert any(reason in r for r in ev.details["reasons"]), (desc, ev.details["reasons"])
    ok = evaluate(dict(base, description="Auto z leasingu, serwisowane w ASO, części z ASO"), v, CFG.criteria, CFG.scoring)
    assert ok.details["criteria_ok"], ok.details["reasons"]
    few = evaluate(dict(base, enriched_at="x", photos=["a"]), v, CFG.criteria, CFG.scoring)
    assert any("za mało zdjęć" in r for r in few.details["reasons"])


def test_valuation_year_window():
    from hunter.scoring import value_from_comparables
    comps = [{"year": 2017, "mileage_km": 100000, "power_hp": 265, "fuel": "diesel", "price_pln": 150000}] * 6
    assert value_from_comparables({"year": 2011, "mileage_km": 200000, "power_hp": 265, "fuel": "diesel"}, comps).value is None
    assert value_from_comparables({"year": 2016, "mileage_km": 120000, "power_hp": 265, "fuel": "diesel"}, comps).value == 150000
