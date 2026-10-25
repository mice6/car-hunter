"""Ocena szkód ze zdjęć: model vision (Qwen3-VL) na RunPod Serverless przez worker vLLM (API OpenAI).

Model tylko KLASYFIKUJE (strefy, części, poduszki, konstrukcja, zalanie...). Kwotę naprawy liczymy
tutaj, z tabeli cen części, bo LLM-y słabo szacują złotówki, a dobrze rozpoznają, co jest rozbite.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import json_repair
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
# Usterki mechaniczne/elektryczne zgłoszone w OPISIE (zdjęcia ich nie pokażą). Koszt warsztatowy, PLN.
MECHANICAL_COSTS: dict[str, int] = {
    "automatic_gearbox": 7000,      # regeneracja/mechatronika automatu
    "manual_gearbox_clutch": 3500,
    "engine_major": 12000,          # silnik do remontu/wymiany
    "engine_missing": 18000,        # brak silnika - używany silnik + montaż
    "engine_minor": 2500,           # osprzęt, wycieki, czujniki
    "turbo": 3500,
    "injectors_fuel": 3000,
    "dpf_egr": 2500,
    "timing_chain_belt": 3500,
    "cooling_system": 1500,
    "electrical_electronics": 2000,
    "air_conditioning": 1500,
    "awd_driveline": 3500,
    "brakes": 1200,
    "warning_light_unknown": 1500,
}
AIRBAGS_SET = 6500        # poduszki czołowe + moduł + napinacze/pasy + naprawa deski
ADAS_FRONT = 1800         # radar/kamera + kalibracja przy uderzeniu w przód
GEOMETRY_DIAG = 900       # geometria, diagnostyka, drobnica
HIDDEN_CONTINGENCY = 0.15

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "photo_observations": {"type": "array", "maxItems": 12,
                               "items": {"type": "string", "maxLength": 160}},
        "notes_pl": {"type": "string", "maxLength": 400},
        "photos_cover": {
            "type": "object",
            "properties": {k: {"type": "boolean"} for k in
                           ["front", "rear", "left", "right", "interior", "engine_bay", "underbody"]},
            "required": ["front", "rear", "left", "right", "interior", "engine_bay", "underbody"],
        },
        "damage_zones": {"type": "array", "maxItems": 13, "items": {
            "type": "object",
            "properties": {"zone": {"enum": ZONES}, "severity": {"enum": SEVERITIES}},
            "required": ["zone", "severity"]}},
        "parts": {"type": "array", "maxItems": 30, "items": {
            "type": "object",
            "properties": {"part": {"enum": list(PART_COSTS)}, "action": {"enum": ["replace", "repair"]}},
            "required": ["part", "action"]}},
        "mechanical_issues": {"type": "array", "maxItems": 8, "items": {"enum": list(MECHANICAL_COSTS)}},
        "airbags_deployed": {"enum": TRI},
        "structural_damage": {"enum": TRI},
        "suspension_damage": {"enum": TRI},
        "flood_signs": {"enum": TRI},
        "fire_signs": {"enum": TRI},
        "drivable": {"enum": TRI},
        "description_consistent": {"enum": ["yes", "no", "unknown"]},
        "overall_severity": {"enum": ["cosmetic", "light", "moderate", "heavy", "total_loss"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["photo_observations", "mechanical_issues", "photos_cover", "damage_zones", "parts", "airbags_deployed", "structural_damage",
                 "suspension_damage", "flood_signs", "fire_signs", "drivable", "description_consistent",
                 "overall_severity", "confidence", "notes_pl"],
}

SYSTEM_PROMPT = """You are an experienced collision-repair estimator in Poland reviewing a used-car listing \
of a DAMAGED car. Report only what is visible or stated; do not guess optimistically.

Work in this order:
1. photo_observations: one short English line per photo, in order: which view it is (e.g. "front-left 3/4", \
"rear", "interior dashboard") and every defect you see on it (dents, creases, cracks, gaps, deployed airbags).
2. Then fill the remaining fields based ONLY on those observations and the seller description.

Rules:
- photos_cover: a side counts as covered if any photo shows it, including 3/4 views (front-left 3/4 covers front \
and left).
- parts: exterior body parts and visible mechanical parts needing work. One entry per physical part (two damaged \
doors = two entries, never the same part twice for one side). "replace" if torn, cracked, deeply creased or \
missing; "repair" for dents/scratches a body shop can fix. A crease or dent on the bonnet means "hood".
- steering_rack, suspension_arm, strut_shock, driveshaft_hub are under the car: list them only if a wheel is \
visibly misaligned or the description says so. A deployed steering-wheel airbag is NOT steering_rack; report \
it via airbags_deployed.
- mechanical_issues: faults stated in the DESCRIPTION (gearbox not shifting, engine knocking, warning light, \
turbo, injectors...). Photos rarely show these; the description is the source.
- airbags_deployed: "yes" if any airbag is deployed, covered with tape/cloth, missing, or the dashboard is torn.
- structural_damage: crushed/bent chassis rails, pillars, sills, roof, or panel gaps that indicate a bent shell.
- flood_signs: silt or water lines in the interior, corrosion on seat rails, mud in the engine bay.
- drivable: from the description first ("odpala, jeździ" = yes; "nie odpala" = no); a gearbox fault = "likely".
- description_consistent: "no" ONLY if photos show clearly worse damage than described; a short or vague \
description that matches the photos is "yes".
- overall_severity must agree with the parts list and airbags (deployed airbags = at least "moderate").
- notes_pl (written right after the observations): 1-3 short, grammatically correct sentences IN POLISH: \
what is damaged and what to check. EVERYTHING you mention in notes_pl (damaged areas, missing engine, \
non-starting engine, airbags) MUST also appear in parts / damage_zones / mechanical_issues / airbags_deployed.
- A missing engine is "engine_missing"; an engine that cranks but does not start is "engine_minor" unless the \
description says the engine is seized or broken ("engine_major").
Answer with compact JSON only (no indentation), matching the schema."""


@dataclass
class VisionConfig:
    api_key: str
    endpoint_id: str
    model: str
    max_images: int = 12
    image_size: str = "1024x768"
    timeout_s: int = 600
    concurrency: int = 4
    inline_images: bool = True   # pobierz zdjęcia lokalnie i wyślij jako base64 (endpoint nie sięga do CDN)

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
    try:
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
    except (ValueError, AttributeError):
        data = json_repair.loads(text)   # ucięta / lekko zepsuta odpowiedź
        if not isinstance(data, dict):
            raise ValueError("unparseable model output") from None
        data["_repaired"] = True
    # sanityzacja: odrzuć nieznane wartości, żeby kosztorys się nie wysypał
    data["parts"] = [p for p in data.get("parts") or [] if isinstance(p, dict) and p.get("part") in PART_COSTS]
    seen, zones = set(), []
    for z in data.get("damage_zones") or []:
        if isinstance(z, dict) and z.get("zone") in ZONES and z["zone"] not in seen:
            seen.add(z["zone"])
            zones.append(z)
    data["damage_zones"] = zones
    # model w pętli powtarza części - każdą część liczymy max 2 razy (lewa/prawa), max 4 dla drzwi/felg/opon
    counts: dict[str, int] = {}
    parts = []
    for p in data["parts"]:
        cap = 4 if p["part"] in ("door", "wheel_rim", "tire", "side_window") else 2
        if counts.get(p["part"], 0) < cap:
            counts[p["part"]] = counts.get(p["part"], 0) + 1
            parts.append(p)
    data["parts"] = parts
    return data


_img_session = None


def fetch_image_data_url(url: str) -> str | None:
    """Pobiera zdjęcie (odcisk Chrome) i zwraca data URL. None, gdy się nie da."""
    global _img_session
    if _img_session is None:
        _img_session = creq.Session(impersonate="chrome")
    try:
        r = _img_session.get(url, timeout=30)
    except Exception as e:  # noqa: BLE001
        log.warning("image %s: %s", url[:80], e)
        return None
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
    if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) < 2000:
        log.warning("image %s: status %s, %s, %d B", url[:80], r.status_code, ctype, len(r.content))
        return None
    return f"data:{ctype};base64," + base64.b64encode(r.content).decode()


def _post_with_retry(cfg: VisionConfig, body: dict) -> dict:
    last = ""
    for attempt in range(1, 4):
        r = creq.post(cfg.base_url + "/chat/completions", json=body, timeout=cfg.timeout_s,
                      headers={"Authorization": f"Bearer {cfg.api_key}"})
        if r.status_code == 200:
            return r.json()
        last = f"RunPod {r.status_code}: {r.text[:300]}"
        if r.status_code in (502, 503, 504, 524) and attempt < 3:
            log.warning("%s - retry %d in %ds", last, attempt, 20 * attempt)
            time.sleep(20 * attempt)
            continue
        break
    raise RuntimeError(last)


NOTE_MECH_HINTS = [
    (r"brak silnika|bez silnika|no engine|engine (?:is )?missing|missing engine", "engine_missing"),
    (r"skrzyni\w* bieg|gearbox|transmission", "automatic_gearbox"),
    (r"nie odpala|nie uruchamia|does not start|doesn't start|won't start|not start", "engine_minor"),
]
ZONE_MIN_COST = {"cosmetic": 400, "light": 1200, "moderate": 3500, "heavy": 8000}

UNDER_CAR_PARTS = {"steering_rack", "suspension_arm", "strut_shock", "driveshaft_hub"}


def sanitize(data: dict, listing: dict) -> dict:
    """Poprawki znanych błędów modelu, na podstawie danych ogłoszenia."""
    # części podwozia tylko gdy model sam zgłasza uszkodzenie zawieszenia
    if data.get("suspension_damage") not in ("yes", "likely"):
        dropped = [p["part"] for p in data.get("parts") or [] if p["part"] in UNDER_CAR_PARTS]
        if dropped:
            data["parts"] = [p for p in data["parts"] if p["part"] not in UNDER_CAR_PARTS]
            data.setdefault("_sanitized", []).append(f"dropped under-car parts: {dropped}")
    # skrzynia: typ wynika z ogłoszenia, nie z modelu
    mech = data.get("mechanical_issues") or []
    gearbox = (listing.get("gearbox") or "").lower()
    if gearbox == "automatic" and "manual_gearbox_clutch" in mech:
        data["mechanical_issues"] = ["automatic_gearbox" if m == "manual_gearbox_clutch" else m for m in mech]
        data.setdefault("_sanitized", []).append("gearbox issue mapped to automatic")
    elif gearbox == "manual" and "automatic_gearbox" in mech:
        data["mechanical_issues"] = ["manual_gearbox_clutch" if m == "automatic_gearbox" else m for m in mech]
    # spójność: usterki wspomniane w notatce/obserwacjach muszą trafić do mechanical_issues
    text = " ".join([data.get("notes_pl") or ""] + list(data.get("photo_observations") or [])).lower()
    mech = list(data.get("mechanical_issues") or [])
    for pattern, issue in NOTE_MECH_HINTS:
        if re.search(pattern, text) and issue not in mech:
            if issue == "automatic_gearbox":
                if not re.search(r"(?:problem|błąd|blad|fault|usterk|nie przełącza|nie przelacza)", text):
                    continue
                issue = "manual_gearbox_clutch" if gearbox == "manual" else "automatic_gearbox"
            if issue == "engine_minor" and {"engine_major", "engine_missing"} & set(mech):
                continue
            mech.append(issue)
            data.setdefault("_sanitized", []).append(f"added {issue} from notes")
    if {"engine_major", "engine_missing"} & set(mech):
        mech = [m for m in mech if m != "engine_minor"]
    if "engine_missing" in mech:
        mech = [m for m in mech if m != "engine_major"]
    data["mechanical_issues"] = mech
    # "kontrolka" zwykle dotyczy tej samej usterki - nie licz podwójnie
    mech = data.get("mechanical_issues") or []
    if "warning_light_unknown" in mech and len(set(mech)) > 1:
        data["mechanical_issues"] = [m for m in mech if m != "warning_light_unknown"]
    return data


def analyze(cfg: VisionConfig, listing: dict) -> dict:
    photos = select_photos(listing.get("photos") or [], cfg.max_images, cfg.image_size)
    if cfg.inline_images:
        photos = [d for d in (fetch_image_data_url(u) for u in photos) if d]
    if not photos:
        return {"_error": "no_photos"}
    body = {
        "model": cfg.model,
        "messages": build_messages(listing, photos),
        "temperature": 0.1,
        "max_tokens": 2500,
        "repetition_penalty": 1.05,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "damage_assessment", "schema": RESPONSE_SCHEMA}},
    }
    started = time.monotonic()
    j = _post_with_retry(cfg, body)
    choice = j["choices"][0]
    content = choice["message"].get("content") or ""
    usage = j.get("usage") or {}
    meta = {"model": cfg.model, "photos": len(photos), "seconds": round(time.monotonic() - started, 1),
            "finish_reason": choice.get("finish_reason"),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")}
    try:
        data = sanitize(parse_response(content), listing)
    except ValueError as e:
        return {"_error": f"parse: {e}", "_raw": content[:4000], "_meta": meta}
    data["_meta"] = meta
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
    mech = [m for m in dict.fromkeys(v.get("mechanical_issues") or []) if m in MECHANICAL_COSTS]
    for m in mech:
        extras += MECHANICAL_COSTS[m]
        breakdown[f"mech:{m}"] = MECHANICAL_COSTS[m]
    if mech:
        flags.append("usterki z opisu: " + ", ".join(mech))
        risk += 0.05 * len(mech) + (0.10 if {"engine_major", "automatic_gearbox"} & set(mech) else 0)
    sev = v.get("overall_severity") or "moderate"
    if v.get("airbags_deployed") in ("yes", "likely") and sev in ("cosmetic", "light"):
        sev = "moderate"   # model bywa niekonsekwentny
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
        risk += 0.05   # model często zaznacza "no" bez powodu
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
    # strefy uszkodzeń jako dolna granica - model bywa skąpy w liście części
    zone_floor = sum(ZONE_MIN_COST.get(z.get("severity"), 0) for z in v.get("damage_zones") or [])
    if zone_floor > parts_cost:
        breakdown["zone_floor"] = zone_floor - parts_cost
        parts_cost = zone_floor
    repair = (parts_cost + extras) * seg * (1 + contingency)
    if sev == "total_loss":
        risk += 0.30
        flags.append("model: szkoda całkowita")
    severity = {"cosmetic": "light", "light": "light", "moderate": "medium", "heavy": "severe",
                "total_loss": "severe"}.get(sev, "medium")
    breakdown.update({"segment_factor": round(seg, 2), "contingency": round(contingency, 2)})
    return VisionEstimate(round(max(repair, 1500), 0), min(risk, 0.9), severity, flags, breakdown)
