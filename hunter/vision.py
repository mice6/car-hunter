"""Ocena szkód ze zdjęć: model vision (Qwen3-VL) na RunPod Serverless przez worker vLLM (API OpenAI).

Model tylko KLASYFIKUJE (strefy, części, poduszki, konstrukcja, zalanie...). Kwotę naprawy liczymy
tutaj, z tabeli cen części, bo LLM-y słabo szacują złotówki, a dobrze rozpoznają, co jest rozbite.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from curl_cffi import requests as creq

log = logging.getLogger(__name__)

ZONES = ["front", "front_left", "front_right", "left", "right", "rear", "rear_left", "rear_right",
         "roof", "underbody", "interior", "engine_bay", "wheels_suspension"]
SEVERITIES = ["cosmetic", "light", "moderate", "heavy"]
TRI = ["yes", "likely", "no", "unknown"]

# Koszt warsztatowy w PLN dla auta klasy średniej (~70 tys. zł zdrowe): część (zamiennik/używka) +
# lakier + robocizna. (wymiana, naprawa). Skalowane współczynnikiem segmentu.
PART_COSTS: dict[str, tuple[int, int]] = {
    "front_bumper": (1800, 700),
    "rear_bumper": (1600, 700),
    "bumper_reinforcement": (800, 300),
    "hood": (2200, 900),
    "front_fender": (1300, 600),
    "front_apron": (1600, 800),           # pas przedni
    "radiator_pack": (2200, 2200),        # chłodnice + intercooler/klima
    "grille": (500, 200),
    "headlight": (1600, 400),
    "fog_light": (250, 100),
    "taillight": (600, 200),
    "door": (2200, 900),
    "side_mirror": (700, 250),
    "rear_quarter_panel": (3800, 1300),   # błotnik tylny (spawany)
    "sill": (2600, 1000),                 # próg
    "pillar": (4500, 1800),               # słupek
    "roof": (5500, 1600),
    "tailgate_trunk": (2600, 1000),
    "windshield": (1500, 1500),
    "side_window": (500, 500),
    "rear_window": (900, 900),
    "wheel_rim": (700, 250),
    "tire": (450, 450),
    "suspension_arm": (900, 900),
    "strut_shock": (1100, 1100),
    "steering_rack": (2500, 1200),
    "driveshaft_hub": (1300, 1300),
    "engine_component": (3000, 1500),
    "interior_trim": (800, 300),
    "dashboard": (3000, 1200),
    "seat": (1500, 600),
}
AIRBAGS_SET = 6500        # poduszki czołowe + moduł + napinacze/pasy + naprawa deski
ADAS_FRONT = 1800         # radar/kamera + kalibracja przy uderzeniu w przód
GEOMETRY_DIAG = 900       # geometria, diagnostyka, drobnica
HIDDEN_CONTINGENCY = 0.15

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "photos_cover": {
            "type": "object",
            "properties": {k: {"type": "boolean"} for k in
                           ["front", "rear", "left", "right", "interior", "engine_bay", "underbody"]},
            "required": ["front", "rear", "left", "right", "interior", "engine_bay", "underbody"],
        },
        "damage_zones": {"type": "array", "items": {
            "type": "object",
            "properties": {"zone": {"enum": ZONES}, "severity": {"enum": SEVERITIES}},
            "required": ["zone", "severity"]}},
        "parts": {"type": "array", "items": {
            "type": "object",
            "properties": {"part": {"enum": list(PART_COSTS)}, "action": {"enum": ["replace", "repair"]}},
            "required": ["part", "action"]}},
        "airbags_deployed": {"enum": TRI},
        "structural_damage": {"enum": TRI},
        "suspension_damage": {"enum": TRI},
        "flood_signs": {"enum": TRI},
        "fire_signs": {"enum": TRI},
        "drivable": {"enum": TRI},
        "description_consistent": {"enum": ["yes", "no", "unknown"]},
        "overall_severity": {"enum": ["cosmetic", "light", "moderate", "heavy", "total_loss"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "notes_pl": {"type": "string", "maxLength": 400},
    },
    "required": ["photos_cover", "damage_zones", "parts", "airbags_deployed", "structural_damage",
                 "suspension_damage", "flood_signs", "fire_signs", "drivable", "description_consistent",
                 "overall_severity", "confidence", "notes_pl"],
}

SYSTEM_PROMPT = """You are an experienced collision-repair estimator in Poland reviewing a used-car listing \
of a DAMAGED car. Look at every photo carefully. Report only what is visible or strongly implied; do not guess \
optimistically. Sellers often hide the worst damage: if a side of the car is not shown, mark it as not covered.

Rules:
- parts: list every exterior/mechanical part that needs work. Repeat a part for each side (two doors = two entries). \
"replace" if torn, cracked, deeply creased, or missing; "repair" for dents/scratches that a body shop can fix.
- airbags_deployed: "yes" if any airbag is visibly deployed or the airbag light/torn dashboard is visible.
- structural_damage: crushed/bent chassis rails, pillars, sills, roof, or panel gaps that indicate a bent shell.
- flood_signs: silt or water lines in the interior, corrosion on seat rails, mud in the engine bay.
- description_consistent: "no" if the photos show clearly worse damage than the seller describes.
- confidence: how sure you are given photo quality and coverage.
- notes_pl: 1-3 short sentences IN POLISH: what is damaged and what to check during inspection.
Answer with JSON only, matching the schema."""


@dataclass
class VisionConfig:
    api_key: str
    endpoint_id: str
    model: str
    max_images: int = 12
    image_size: str = "1024x768"
    timeout_s: int = 600
    concurrency: int = 4

    @property
    def base_url(self) -> str:
        return f"https://api.runpod.ai/v2/{self.endpoint_id}/openai/v1"


def select_photos(photos: list[str], max_images: int, size: str) -> list[str]:
    """Pierwsze 8 (sprzedawcy zwykle zaczynają od uszkodzeń/ujęć ogólnych) + równomiernie z reszty."""
    photos = [p for p in photos if p]
    if len(photos) > max_images:
        head = photos[:8]
        rest = photos[8:]
        k = max_images - len(head)
        step = len(rest) / k
        photos = head + [rest[int(i * step)] for i in range(k)]
    out = []
    for p in photos:
        if re.search(r";s=\d+x\d+", p):
            p = re.sub(r";s=\d+x\d+", f";s={size}", p)
        elif p.endswith("/image"):
            p = f"{p};s={size}"
        out.append(p)
    return out


def build_messages(listing: dict, photos: list[str]) -> list[dict]:
    desc = (listing.get("description") or "").strip()[:2000]
    facts = ", ".join(str(x) for x in [
        f"{listing.get('make') or ''} {listing.get('model') or ''}".strip(), listing.get("year"),
        f"{listing['mileage_km']} km" if listing.get("mileage_km") else None,
        f"{listing['power_hp']} HP" if listing.get("power_hp") else None, listing.get("fuel"),
        listing.get("body_type"),
    ] if x)
    text = (f"Listing title: {listing.get('title') or ''}\nCar: {facts}\n"
            f"Seller description (Polish, may be biased):\n{desc or '(none)'}\n\n"
            f"{len(photos)} photos follow.")
    content = [{"type": "text", "text": text}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in photos]
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]


def parse_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    # sanityzacja: odrzuć nieznane wartości, żeby kosztorys się nie wysypał
    data["parts"] = [p for p in data.get("parts") or [] if p.get("part") in PART_COSTS]
    data["damage_zones"] = [z for z in data.get("damage_zones") or [] if z.get("zone") in ZONES]
    return data


def analyze(cfg: VisionConfig, listing: dict) -> dict:
    photos = select_photos(listing.get("photos") or [], cfg.max_images, cfg.image_size)
    if not photos:
        return {"_error": "no_photos"}
    body = {
        "model": cfg.model,
        "messages": build_messages(listing, photos),
        "temperature": 0.1,
        "max_tokens": 900,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "damage_assessment", "schema": RESPONSE_SCHEMA}},
    }
    r = creq.post(cfg.base_url + "/chat/completions", json=body, timeout=cfg.timeout_s,
                  headers={"Authorization": f"Bearer {cfg.api_key}"})
    if r.status_code != 200:
        raise RuntimeError(f"RunPod {r.status_code}: {r.text[:300]}")
    j = r.json()
    data = parse_response(j["choices"][0]["message"]["content"])
    usage = j.get("usage") or {}
    data["_meta"] = {"model": cfg.model, "photos": len(photos),
                     "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}
    return data


def analyze_many(cfg: VisionConfig, listings: list[dict], on_result) -> dict:
    stats = {"ok": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs = {ex.submit(analyze, cfg, lst): lst for lst in listings}
        for f in as_completed(futs):
            lst = futs[f]
            try:
                res = f.result()
                stats["error" if "_error" in res else "ok"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("vision %s failed: %s", lst.get("id"), e)
                res = {"_error": str(e)[:300]}
                stats["error"] += 1
            on_result(lst, res)
    return stats


# ------------------------------------------------------------------ kosztorys z wyniku vision

@dataclass
class VisionEstimate:
    repair_pln: float
    risk: float
    severity: str
    flags: list[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)


def segment_factor(market_value: float | None) -> float:
    if not market_value:
        return 1.0
    return min(max((market_value / 70000) ** 0.5, 0.75), 1.6)


def estimate_repair(v: dict, market_value: float | None) -> VisionEstimate:
    seg = segment_factor(market_value)
    breakdown: dict[str, float] = {}
    parts_cost = 0.0
    for p in v.get("parts") or []:
        replace_cost, repair_cost = PART_COSTS[p["part"]]
        c = replace_cost if p.get("action") == "replace" else repair_cost
        parts_cost += c
        key = f"{p['part']}:{p.get('action')}"
        breakdown[key] = breakdown.get(key, 0) + c

    extras = GEOMETRY_DIAG
    flags: list[str] = []
    risk = 0.10
    if v.get("airbags_deployed") in ("yes", "likely"):
        extras += AIRBAGS_SET
        breakdown["airbags"] = AIRBAGS_SET
        flags.append("poduszki wystrzelone" if v["airbags_deployed"] == "yes" else "możliwe poduszki")
        risk += 0.10
    zones = {z["zone"]: z["severity"] for z in v.get("damage_zones") or []}
    if any(zones.get(z) in ("moderate", "heavy") for z in ("front", "front_left", "front_right")):
        extras += ADAS_FRONT
        breakdown["adas_front"] = ADAS_FRONT

    structural = v.get("structural_damage")
    if structural == "yes":
        risk += 0.35
        flags.append("uszkodzenia konstrukcji")
    elif structural == "likely":
        risk += 0.20
        flags.append("prawdopodobnie konstrukcja")
    if v.get("flood_signs") in ("yes", "likely"):
        risk += 0.35
        flags.append("ślady zalania")
    if v.get("fire_signs") in ("yes", "likely"):
        risk += 0.40
        flags.append("ślady ognia")
    if v.get("suspension_damage") in ("yes", "likely"):
        risk += 0.08
        flags.append("zawieszenie")
    if v.get("drivable") == "no":
        risk += 0.08
        flags.append("nie jeździ")
    if v.get("description_consistent") == "no":
        risk += 0.12
        flags.append("zdjęcia gorsze niż opis")

    cover = v.get("photos_cover") or {}
    missing = [k for k in ("front", "rear", "left", "right") if not cover.get(k)]
    if missing:
        risk += 0.04 * len(missing)
        flags.append("brak zdjęć: " + ", ".join(missing))
    conf = float(v.get("confidence") or 0.5)
    if conf < 0.5:
        risk += 0.10
        flags.append(f"niska pewność ({conf:.0%})")

    contingency = HIDDEN_CONTINGENCY + 0.05 * len(missing) + (0.10 if structural in ("yes", "likely") else 0)
    repair = (parts_cost + extras) * seg * (1 + contingency)
    sev = v.get("overall_severity") or "moderate"
    if sev == "total_loss":
        risk += 0.30
        flags.append("model: szkoda całkowita")
    severity = {"cosmetic": "light", "light": "light", "moderate": "medium", "heavy": "severe",
                "total_loss": "severe"}.get(sev, "medium")
    breakdown.update({"segment_factor": round(seg, 2), "contingency": round(contingency, 2)})
    return VisionEstimate(round(max(repair, 1500), 0), min(risk, 0.9), severity, flags, breakdown)
