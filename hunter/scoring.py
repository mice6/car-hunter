"""Wycena rynkowa, wstępny kosztorys i scoring.

Wycena (krok 1-2): regresja log(ceny) po roczniku, przebiegu i mocy na zdrowych ogłoszeniach
tego samego modelu; przy małej próbie - ważona mediana najbliższych. Docelowo zastąpi ją
globalny model CatBoost (hunter/valuation, krok 2).

Kosztorys (tymczasowy): heurystyka ze słów kluczowych tytułu/opisu. Zastąpi go analiza zdjęć
modelem vision na RunPod (krok 3) - pole listings.vision ma wtedy pierwszeństwo.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------- wycena


@dataclass
class Valuation:
    value: float | None
    method: str
    n: int


def _weighted_median(values: list[float], weights: list[float]) -> float:
    order = np.argsort(values)
    v = np.asarray(values)[order]
    w = np.asarray(weights)[order]
    cw = np.cumsum(w)
    return float(v[np.searchsorted(cw, cw[-1] / 2)])


def value_from_comparables(target: dict, comps: list[dict], min_n: int = 5, year_window: int = 2) -> Valuation:
    """target/comps: dict z year, mileage_km, power_hp, fuel, price_pln."""
    comps = [c for c in comps if c.get("price_pln") and c.get("year")]
    if target.get("year"):
        # tylko sąsiednie roczniki - inaczej 2011 wyceniamy po autach z 2016
        comps = [c for c in comps if abs(c["year"] - target["year"]) <= year_window]
    if not comps:
        return Valuation(None, "none", 0)

    # preferuj ten sam rodzaj paliwa i zbliżoną moc (ta sama wersja silnikowa)
    def subset(fuel=True, power_tol=0.15, year_tol=99):
        out = []
        for c in comps:
            if fuel and target.get("fuel") and c.get("fuel") and c["fuel"] != target["fuel"]:
                continue
            if power_tol and target.get("power_hp") and c.get("power_hp"):
                if abs(c["power_hp"] - target["power_hp"]) > power_tol * target["power_hp"]:
                    continue
            if abs(c["year"] - (target.get("year") or c["year"])) > year_tol:
                continue
            out.append(c)
        return out

    for args in ({"fuel": True, "power_tol": 0.15}, {"fuel": True, "power_tol": 0.30},
                 {"fuel": False, "power_tol": 0.30}, {"fuel": False, "power_tol": None}):
        pool = subset(**args)
        if len(pool) >= max(15, min_n) and target.get("year") and target.get("mileage_km"):
            v = _regression(target, pool)
            if v:
                return Valuation(v, f"regression(fuel={args['fuel']},pwr={args['power_tol']})", len(pool))
        if len(pool) >= min_n:
            return Valuation(_nearest_median(target, pool), f"nearest_median(fuel={args['fuel']},pwr={args['power_tol']})", len(pool))
    return Valuation(None, "too_few_comparables", len(comps))


def _regression(target: dict, pool: list[dict]) -> float | None:
    rows = [c for c in pool if c.get("mileage_km") is not None]
    if len(rows) < 15:
        return None
    use_power = all(c.get("power_hp") for c in rows) and target.get("power_hp")
    X = []
    for c in rows:
        x = [1.0, c["year"], math.log1p(c["mileage_km"])]
        if use_power:
            x.append(math.log(c["power_hp"]))
        X.append(x)
    X = np.asarray(X)
    y = np.log(np.asarray([float(c["price_pln"]) for c in rows]))
    # odporność na wartości odstające: dwa przebiegi, drugi bez reszt > 2.5 sigma
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    keep = np.abs(resid) < 2.5 * (resid.std() or 1)
    if keep.sum() >= 12:
        coef, *_ = np.linalg.lstsq(X[keep], y[keep], rcond=None)
    t = [1.0, target["year"], math.log1p(target["mileage_km"])]
    if use_power:
        t.append(math.log(target["power_hp"]))
    pred = float(math.exp(np.asarray(t) @ coef))
    prices = np.asarray([float(c["price_pln"]) for c in rows])
    lo, hi = np.percentile(prices, 5), np.percentile(prices, 95)
    return float(min(max(pred, lo * 0.85), hi * 1.15))


def _nearest_median(target: dict, pool: list[dict]) -> float:
    weights = []
    for c in pool:
        d = abs(c["year"] - (target.get("year") or c["year"]))
        if target.get("mileage_km") and c.get("mileage_km") is not None:
            d += abs(c["mileage_km"] - target["mileage_km"]) / 40000
        weights.append(1.0 / (1.0 + d))
    return _weighted_median([float(c["price_pln"]) for c in pool], weights)


# ---------------------------------------------------------------- szkody i ryzyko

SEVERE = [
    r"poduszk", r"airbag", r"podłużnic", r"podluznic", r"słupk", r"slupk", r"zalan", r"flood",
    r"powódź", r"powodz", r"pożar", r"pozar", r"spalon", r"nie odpala", r"nie uruchamia", r"silnik do",
    r"skrzynia do", r"uszkodzon\w* silnik", r"uszkodzon\w* skrzyni", r"rozbit", r"kasacj", r"dachowan",
    r"uszkodzon\w* dach", r"dach do wymiany", r"\brama\b", r"krzyw\w* ram", r"geometri\w* nadwozia",
    r"strukturaln",
]
LIGHT = [
    r"lekko", r"drobn", r"kosmetyczn", r"obtar", r"\brys[ay]?\b", r"\bryski", r"wgnie", r"parkingow",
    r"otarci", r"\bgrad", r"tylko (?:zderzak|błotnik|drzwi)", r"jeżdżąc", r"jezdzac", r"sprawn\w* technicznie",
]
FRONT = [r"przód", r"przod\b", r"maska", r"chłodnic", r"chlodnic", r"reflektor", r"pas przedni", r"czołow"]
US_IMPORT = [r"\busa\b", r"copart", r"iaai", r"jedzie z usa", r"z ameryki", r"carfax", r"autocheck"]
PARTS_ONLY = [r"na częś", r"na czesc", r"dawc", r"rozbiórk", r"rozbiork", r"tylko częś", r"tylko czesc",
              r"w całości na", r"w calosci na"]
NOT_A_PRICE = [r"\bcesj", r"przejęci\w* leasing", r"przejeci\w* leasing", r"odstępn", r"odstepn", r"licytacj", r"cena wywoławcz", r"cena wywolawcz",
               r"zaliczk", r"\bdo wykupu", r"przejęci\w* rat", r"przejeci\w* rat"]
NO_DOCS = [r"bez dokument", r"brak dokument", r"bez papier", r"brak papier", r"bez dowodu", r"brak dowodu",
           r"bez karty", r"salvage", r"\bparts only\b"]
FLOOD = [r"zalan", r"flood", r"powódź", r"powodz", r"water damage"]


def _hits(text: str, patterns: list[str]) -> list[str]:
    out = []
    for p in patterns:
        m = re.search(p + r"\w*", text, re.I)
        if m:
            out.append(m.group(0).lower())
    return out


@dataclass
class DamageAssessment:
    severity: str                  # light | medium | severe
    repair_fraction: float         # część wartości rynkowej (heurystyka)
    risk: float                    # 0..1
    flags: list[str] = field(default_factory=list)
    source: str = "keywords"
    repair_pln: float | None = None  # kwota z kosztorysu vision (ma pierwszeństwo)
    notes: str | None = None
    breakdown: dict | None = None


def _context_risk(listing: dict, text: str, with_vision: bool) -> tuple[float, list[str]]:
    """Ryzyka niezależne od zdjęć: pochodzenie, rejestracja, jakość ogłoszenia."""
    risk, flags = 0.0, []
    if _hits(text, US_IMPORT):
        risk += 0.20
        flags.append("import z USA")
    if not with_vision and _hits(text, FLOOD):
        risk += 0.25
        flags.append("możliwe zalanie")
    if listing.get("has_registration") is False:
        risk += 0.10
        flags.append("brak rejestracji w PL")
    photos = listing.get("photos") or []
    if listing.get("enriched_at") and len(photos) < 6:
        risk += 0.10
        flags.append(f"mało zdjęć ({len(photos)})")
    if not (listing.get("description") or "").strip() and listing.get("enriched_at"):
        risk += 0.05
        flags.append("brak opisu")
    return risk, flags


def assess_damage(listing: dict, market_value: float | None = None) -> DamageAssessment:
    """Wynik modelu vision (listing['vision']) ma pierwszeństwo; inaczej heurystyka ze słów kluczowych."""
    text = " ".join(filter(None, [listing.get("title"), listing.get("short_description"), listing.get("description")]))
    vision = listing.get("vision") or None
    if vision and "_error" not in vision and vision.get("parts") is not None:
        from .vision import estimate_repair
        est = estimate_repair(vision, market_value)
        crisk, cflags = _context_risk(listing, text, with_vision=True)
        return DamageAssessment(
            severity=est.severity, repair_fraction=(est.repair_pln / market_value) if market_value else 0.0,
            risk=min(est.risk + crisk, 0.9), flags=est.flags + cflags, source="vision",
            repair_pln=est.repair_pln, notes=vision.get("notes_pl"), breakdown=est.breakdown,
        )

    severe, light, front = _hits(text, SEVERE), _hits(text, LIGHT), _hits(text, FRONT)
    flags: list[str] = []
    risk = 0.10

    if severe:
        severity, frac = "severe", 0.35
        risk += 0.30
        flags.append("ciężka szkoda: " + ", ".join(sorted(set(severe))[:4]))
    elif light and not front:
        severity, frac = "light", 0.06
    elif front:
        severity, frac = "medium", 0.20      # przód: chłodnice, ADAS, poduszki - drożej
        flags.append("uderzenie w przód")
    else:
        severity, frac = "medium", 0.15

    crisk, cflags = _context_risk(listing, text, with_vision=False)
    return DamageAssessment(severity, frac, min(risk + crisk, 0.9), flags + cflags)


# ---------------------------------------------------------------- scoring


@dataclass
class Evaluation:
    market_value: float | None
    valuation_method: str
    comparables_n: int
    repair_estimate: float | None
    fees: float | None
    total_cost: float | None
    savings: float | None
    risk: float
    score: float | None
    passes_filters: bool
    details: dict


def _lower(xs) -> set[str]:
    return {str(x).lower().strip() for x in (xs or [])}


def criteria_failures(listing: dict, crit: dict) -> list[str]:
    """Twarde wymagania z config.yaml -> lista powodów odrzucenia (pusta = spełnia)."""
    out = []
    make = (listing.get("make") or "").lower()
    model = (listing.get("model") or "").lower()
    if listing.get("price_pln") is None:
        out.append("brak ceny")
    if (listing.get("power_hp") or 0) < (crit.get("min_power_hp") or 0):
        out.append("za słaby silnik")
    if crit.get("gearbox") and listing.get("gearbox") and listing["gearbox"] != crit["gearbox"]:
        out.append(f"skrzynia {listing['gearbox']}")
    year = listing.get("year")
    if year and crit.get("min_year") and year < crit["min_year"]:
        out.append("za stary")
    if year and crit.get("max_year") and year > crit["max_year"]:
        out.append("za nowy")
    if crit.get("max_mileage_km") and (listing.get("mileage_km") or 0) > crit["max_mileage_km"]:
        out.append("za duży przebieg")
    if crit.get("fuels") and listing.get("fuel") not in _lower(crit["fuels"]):
        out.append(f"paliwo {listing.get('fuel')}")
    if crit.get("bodies") and listing.get("body_type") not in _lower(crit["bodies"]):
        out.append(f"nadwozie {listing.get('body_type')}")
    if crit.get("include_makes") and make not in _lower(crit["include_makes"]):
        out.append("marka spoza listy")
    if make in _lower(crit.get("exclude_makes")):
        out.append("marka wykluczona")
    inc = _lower(crit.get("include_models"))
    if inc and f"{make}/{model}" not in inc:
        out.append("model spoza listy")
    if f"{make}/{model}" in _lower(crit.get("exclude_models")):
        out.append("model wykluczony")
    if crit.get("regions") and (listing.get("region") or "").lower() not in _lower(crit["regions"]):
        out.append("region")
    if crit.get("exclude_rhd") and listing.get("rhd"):
        out.append("anglik (RHD)")
    if crit.get("exclude_unregistered") and listing.get("has_registration") is False:
        out.append("niezarejestrowany")
    text = " ".join(filter(None, [listing.get("title"), listing.get("short_description"), listing.get("description")]))
    if crit.get("exclude_us_imports") and _hits(text, US_IMPORT):
        out.append("import z USA")
    if _hits(text, PARTS_ONLY):
        out.append("na części / dawca")
    if _hits(text, NOT_A_PRICE):
        out.append("cesja/licytacja/zaliczka - cena nie jest ceną auta")
    if _hits(text, NO_DOCS):
        out.append("bez dokumentów")
    min_photos = crit.get("min_photos", 3)
    if listing.get("enriched_at") and len(listing.get("photos") or []) < min_photos:
        out.append(f"za mało zdjęć ({len(listing.get('photos') or [])})")
    return out


def evaluate(listing: dict, valuation: Valuation, crit: dict, sc: dict, prefs: dict | None = None) -> Evaluation:
    price = float(listing["price_pln"]) if listing.get("price_pln") is not None else None
    dmg = assess_damage(listing, valuation.value)
    details: dict = {"damage": dmg.__dict__, "reasons": []}

    reasons = details["reasons"]
    reasons.extend(criteria_failures(listing, crit))

    if valuation.value is None or price is None:
        details["criteria_ok"] = not reasons
        reasons.append("brak wyceny rynkowej")
        return Evaluation(None, valuation.method, valuation.n, None, None, None, None, dmg.risk, None, False, details)

    # Cena ułamkiem rynku = prawie zawsze wrak, części, licytacja albo fałszywa cena.
    ratio = price / valuation.value
    details["price_ratio"] = round(ratio, 3)
    if ratio < sc.get("min_price_ratio", 0.2):
        reasons.append(f"podejrzanie niska cena ({ratio:.0%} rynku)")
    details["criteria_ok"] = not reasons

    market = valuation.value * sc["asking_to_transaction"] * (1 - sc.get("damage_history_discount", 0))
    if dmg.repair_pln is not None:
        repair = dmg.repair_pln
    else:
        # Bez zdjęć: sprzedawca zna szkodę lepiej niż słowa kluczowe. Zakładamy, że część różnicy
        # cena-rynek to realna szkoda (rynek wycenia ją lepiej niż nasza heurystyka).
        heuristic = valuation.value * dmg.repair_fraction
        implied = sc.get("implied_damage_share", 0.5) * max(0.0, valuation.value - price)
        repair = max(2500.0, heuristic, implied)
        details["repair_basis"] = "implied" if implied > heuristic else "keywords"
    f = sc["fees"]
    fees = price * f["pcc_rate"] + f["registration_inspection_pln"] + f["transport_pln"]
    if listing.get("has_registration") is False or listing.get("imported"):
        fees += f["unregistered_import_pln"]
    total = price + repair + fees
    savings = market - total

    prefs = prefs if prefs is not None else {"bodies": crit.get("preferred_bodies"), "fuels": crit.get("preferred_fuels")}
    bonus = 0.0
    if listing.get("body_type") and listing["body_type"] in _lower(prefs.get("bodies")):
        bonus += sc["pref_bonus_pln"]
        details["reasons"].append(f"+ nadwozie {listing['body_type']}")
    if listing.get("fuel") and listing["fuel"] in _lower(prefs.get("fuels")):
        bonus += sc["pref_bonus_pln"] * 0.5
        details["reasons"].append(f"+ paliwo {listing['fuel']}")
    if listing.get("make") and listing["make"].lower() in _lower(prefs.get("makes")):
        bonus += sc["pref_bonus_pln"]
        details["reasons"].append(f"+ marka {listing['make']}")

    # oszczędność skorygowana o ryzyko (ujemnej nie "poprawiamy" ryzykiem)
    score = (savings * (1 - dmg.risk) if savings > 0 else savings) + bonus

    if total > crit["budget_total_pln"]:
        reasons.append(f"przekracza budżet ({total:,.0f} zł)".replace(",", " "))
    if savings <= 0:
        reasons.append("brak oszczędności")

    hard_fail = [r for r in reasons if not r.startswith("+")]
    details["market_raw"] = valuation.value
    return Evaluation(
        market_value=round(market, 0), valuation_method=valuation.method, comparables_n=valuation.n,
        repair_estimate=round(repair, 0), fees=round(fees, 0), total_cost=round(total, 0),
        savings=round(savings, 0), risk=round(dmg.risk, 3), score=round(score, 0),
        passes_filters=not hard_fail, details=details,
    )
