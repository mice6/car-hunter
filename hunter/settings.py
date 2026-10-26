"""Opis edytowalnych ustawień + parsowanie formularza + bezpieczny zapis config.yaml."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

FUELS = ["diesel", "petrol", "petrol-lpg", "hybrid", "plugin-hybrid", "electric"]
BODIES = ["suv", "combi", "sedan", "compact", "minivan", "coupe", "cabrio", "city-car", "pickup"]
REGIONS = ["Dolnośląskie", "Kujawsko-pomorskie", "Lubelskie", "Lubuskie", "Łódzkie", "Małopolskie", "Mazowieckie",
           "Opolskie", "Podkarpackie", "Podlaskie", "Pomorskie", "Śląskie", "Świętokrzyskie",
           "Warmińsko-mazurskie", "Wielkopolskie", "Zachodniopomorskie"]


@dataclass
class Field:
    section: str
    key: str
    kind: str                      # int | float | bool | choice | multi | list
    label: str
    help: str = ""
    options: list[str] = field(default_factory=list)
    nullable: bool = False
    min: float | None = None
    max: float | None = None

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"


GROUPS: list[tuple[str, str, list[Field]]] = [
    ("auto", "Jakie auto", [
        Field("criteria", "min_power_hp", "int", "Moc min. [KM]", min=0, max=1000),
        Field("criteria", "gearbox", "choice", "Skrzynia", options=["automatic", "manual", ""],
              help="puste = dowolna"),
        Field("criteria", "min_year", "int", "Rocznik od", min=1990, max=2030,
              help="obniżenie działa w pełni po najbliższym pełnym przebiegu (do 24 h)"),
        Field("criteria", "max_year", "int", "Rocznik do", nullable=True, min=1990, max=2030),
        Field("criteria", "max_mileage_km", "int", "Przebieg max [km]", nullable=True, min=0),
        Field("criteria", "fuels", "multi", "Paliwo (wymagane)", options=FUELS, help="nic nie zaznaczone = dowolne"),
        Field("criteria", "bodies", "multi", "Nadwozie (wymagane)", options=BODIES, help="nic = dowolne"),
        Field("criteria", "include_makes", "list", "Tylko marki", help="np. kia, volvo, bmw - zawęża też pobieranie"),
        Field("criteria", "exclude_makes", "list", "Bez marek", help="np. dodge, chrysler"),
        Field("criteria", "include_models", "list", "Tylko modele", help="marka/model, np. kia/stinger, volvo/xc60"),
        Field("criteria", "exclude_models", "list", "Bez modeli", help="np. ford/ranger"),
        Field("criteria", "regions", "multi", "Województwa", options=REGIONS, help="nic = cała Polska"),
    ]),
    ("budzet", "Budżet", [
        Field("criteria", "budget_total_pln", "int", "Budżet łącznie [zł]", help="zakup + naprawa + opłaty", min=1000),
        Field("criteria", "max_purchase_pln", "int", "Cena ogłoszenia max [zł]", min=1000),
    ]),
    ("szkoda", "Szkoda i ryzyko", [
        Field("criteria", "min_price_ratio", "float", "Cena min. [% rynku]", min=0, max=1,
              help="0.35 = tańsze niż 35% wartości zdrowego to wrak"),
        Field("criteria", "max_damage", "choice", "Szkoda max (ze zdjęć)", options=["light", "moderate", "heavy"]),
        Field("criteria", "exclude_airbags", "bool", "Odrzuć: wystrzelone poduszki"),
        Field("criteria", "exclude_structural", "bool", "Odrzuć: uszkodzona konstrukcja"),
        Field("criteria", "exclude_flood_fire", "bool", "Odrzuć: zalanie / pożar"),
        Field("criteria", "exclude_not_drivable", "bool", "Odrzuć: nie jeździ"),
        Field("criteria", "exclude_us_imports", "bool", "Odrzuć: import z USA"),
        Field("criteria", "exclude_unregistered", "bool", "Odrzuć: niezarejestrowane w PL"),
        Field("criteria", "exclude_rhd", "bool", "Odrzuć: anglik (RHD)"),
        Field("criteria", "min_photos", "int", "Zdjęć min.", min=0, max=50),
    ]),
    ("pref", "Preferencje (premia, nie wymóg)", [
        Field("preferences", "bodies", "multi", "Nadwozie", options=BODIES),
        Field("preferences", "fuels", "multi", "Paliwo", options=FUELS),
        Field("preferences", "makes", "list", "Marki", help="np. volvo, mazda"),
        Field("scoring", "pref_bonus_pln", "int", "Premia [zł]", min=0),
    ]),
    ("perla", "💎 Perełka (alert tylko gdy spełnione wszystko)", [
        Field("alerts", "gems_only", "bool", "Telegram tylko dla perełek"),
        Field("scoring", "gem.min_savings_pln", "int", "Oszczędność min. [zł]", min=0,
              help="po naprawie i opłatach, względem zdrowego egzemplarza"),
        Field("scoring", "gem.min_ratio", "float", "Cena od [% rynku]", min=0, max=1, help="0.5 = 50%"),
        Field("scoring", "gem.max_ratio", "float", "Cena do [% rynku]", min=0, max=1),
        Field("scoring", "gem.min_comparables", "int", "Porównań min.", min=3, max=200, help="pewność wyceny"),
        Field("scoring", "gem.max_risk", "float", "Ryzyko max", min=0, max=1),
        Field("scoring", "gem.require_drivable", "bool", "Wymagaj: jeździ / odpala"),
        Field("scoring", "gem.require_registered", "bool", "Wymagaj: zarejestrowany w PL"),
        Field("scoring", "gem.exclude_us_imports", "bool", "Bez importów z USA"),
        Field("alerts", "max_per_cycle", "int", "Alertów max na cykl", min=1, max=20),
    ]),
    ("alerty", "Alerty i ocena zdjęć", [
        Field("scoring", "min_score_alert_pln", "int", "Alert od score [zł] (gdy nie tylko perełki)", min=0),
        Field("vision", "auto", "bool", "Automatyczna ocena zdjęć (RunPod)"),
        Field("vision", "mode", "choice", "Co wysyłać na RunPod", options=["gems", "all"],
              help="gems = tylko kandydaci na perełkę + top N; all = cała kolejka (drożej)"),
        Field("vision", "top_n", "int", "Top N rankingu zawsze ocenione", min=0, max=50),
        Field("vision", "require_for_alert", "bool", "Alert tylko po potwierdzeniu zdjęciami"),
        Field("vision", "every_hours", "float", "Pakiet co [h]", min=0.5, max=48),
        Field("vision", "max_per_run", "int", "Aut w pakiecie", min=1, max=200),
        Field("vision", "max_per_day", "int", "Aut dziennie max", min=0, max=2000, help="twardy limit kosztów"),
    ]),
    ("zaawansowane", "Zaawansowane", [
        Field("scoring", "implied_damage_share", "float", "Bez zdjęć: szkoda = x · (rynek − cena)", min=0, max=1),
        Field("scoring", "vision_min_gap_share", "float", "Ze zdjęciami: naprawa ≥ x · (rynek − cena)", min=0, max=1),
        Field("scoring", "asking_to_transaction", "float", "Cena ofertowa → transakcyjna", min=0.5, max=1),
        Field("scoring", "valuation_year_window", "int", "Wycena z roczników ±", min=0, max=6),
        Field("schedule", "candidates_every_min", "int", "Cykl pobierania co [min]", min=10, max=720),
    ]),
]

ALL_FIELDS = [f for _, _, fs in GROUPS for f in fs]


def get_value(data: dict, f: Field) -> Any:
    cur: Any = data.get(f.section) or {}
    for part in f.key.split("."):
        cur = (cur or {}).get(part) if isinstance(cur, dict) else None
    return cur


def _set(out: dict, section: str, key: str, val: Any) -> None:
    cur = out.setdefault(section, {})
    parts = key.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = val


class FormError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def parse_form(form: dict[str, Any]) -> dict:
    """form: {name: str | list[str]} -> {section: {key: value}}. Rzuca FormError."""
    out: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def getlist(name):
        v = form.get(name)
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    for f in ALL_FIELDS:
        raw = form.get(f.name)
        if isinstance(raw, list) and f.kind not in ("multi",):
            raw = raw[-1]
        try:
            if f.kind == "bool":
                val: Any = raw in ("on", "true", "1", True)
            elif f.kind == "multi":
                val = [v for v in getlist(f.name) if v in f.options]
            elif f.kind == "list":
                val = [x.strip().lower() for x in str(raw or "").replace(";", ",").split(",") if x.strip()]
            elif f.kind == "choice":
                val = (raw or "").strip()
                if val not in f.options:
                    raise ValueError("niedozwolona wartość")
                val = val or None
            else:
                txt = str(raw or "").strip().replace(",", ".").replace(" ", "")
                if not txt:
                    if f.nullable:
                        val = None
                    else:
                        raise ValueError("wymagane")
                else:
                    val = int(float(txt)) if f.kind == "int" else float(txt)
                    if f.min is not None and val < f.min:
                        raise ValueError(f"min. {f.min:g}")
                    if f.max is not None and val > f.max:
                        raise ValueError(f"max. {f.max:g}")
        except ValueError as e:
            errors[f.name] = str(e) if str(e) else "zła wartość"
            continue
        _set(out, f.section, f.key, val)

    if not errors:
        c = out.get("criteria", {})
        if c.get("max_year") and c.get("min_year") and c["max_year"] < c["min_year"]:
            errors["criteria.max_year"] = "mniejszy niż 'od'"
        if c.get("max_purchase_pln") and c.get("budget_total_pln") and c["max_purchase_pln"] > c["budget_total_pln"]:
            errors["criteria.max_purchase_pln"] = "większa niż budżet"
        for m in c.get("include_models", []) + c.get("exclude_models", []):
            if "/" not in m:
                errors["criteria.include_models"] = f"'{m}' - format marka/model"
    if errors:
        raise FormError(errors)
    return out


def merge_into(user_cfg: dict, changes: dict) -> dict:
    def deep(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in b.items():
            out[k] = deep(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out
    return deep(user_cfg or {}, changes)


def save_config(path: str | Path, changes: dict, keep_backups: int = 20) -> Path:
    """Scala zmiany z istniejącym config.yaml (inne klucze zostają), zapis atomowy + kopia."""
    path = Path(path)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    merged = merge_into(current or {}, changes)
    text = ("# Edytowane przez panel car-hunter " + datetime.now().strftime("%Y-%m-%d %H:%M") + "\n"
            + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False))
    yaml.safe_load(text)  # sanity

    if path.exists():
        bdir = path.parent / "config-backups"
        bdir.mkdir(exist_ok=True)
        shutil.copy2(path, bdir / f"config-{datetime.now():%Y%m%d-%H%M%S}.yaml")
        for old in sorted(bdir.glob("config-*.yaml"))[:-keep_backups]:
            old.unlink(missing_ok=True)

    # bind-mount pliku: rename na plik nie zawsze działa (EBUSY) -> wtedy zapis w miejscu
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.replace(tmp, path)
    except OSError:
        os.unlink(tmp)
        path.write_text(text, encoding="utf-8")
    return path
