import json

from hunter import vision
from hunter.config import load_config
from hunter.scoring import Valuation, assess_damage, evaluate

CFG = load_config("/nonexistent.yaml")

LIGHT = {
    "photos_cover": {"front": True, "rear": True, "left": True, "right": True, "interior": True,
                     "engine_bay": False, "underbody": False},
    "damage_zones": [{"zone": "rear", "severity": "light"}],
    "parts": [{"part": "rear_bumper", "action": "replace"}, {"part": "taillight", "action": "replace"}],
    "airbags_deployed": "no", "structural_damage": "no", "suspension_damage": "no", "flood_signs": "no",
    "fire_signs": "no", "drivable": "yes", "description_consistent": "yes", "overall_severity": "light",
    "confidence": 0.85, "notes_pl": "Uszkodzony zderzak tylny i lampa.",
}

HEAVY = dict(LIGHT, damage_zones=[{"zone": "front", "severity": "heavy"}],
             parts=[{"part": p, "action": "replace"} for p in
                    ["front_bumper", "hood", "front_fender", "front_fender", "headlight", "headlight",
                     "radiator_pack", "front_apron"]],
             airbags_deployed="yes", structural_damage="likely", overall_severity="heavy",
             photos_cover=dict(LIGHT["photos_cover"], rear=False))


def test_select_photos_resizes_and_samples():
    photos = [f"https://cdn/x{i}/image;s=1280x960" for i in range(30)] + ["https://cdn/y/image"]
    sel = vision.select_photos(photos, 12, "1024x768")
    assert len(sel) == 12 and sel[0] == "https://cdn/x0/image;s=1024x768"
    assert all(";s=1024x768" in s for s in sel)
    assert vision.select_photos(["https://cdn/y/image"], 12, "800x600") == ["https://cdn/y/image;s=800x600"]


def test_estimate_light_vs_heavy():
    light = vision.estimate_repair(LIGHT, 70000)
    heavy = vision.estimate_repair(HEAVY, 70000)
    assert 2500 < light.repair_pln < 5000
    assert heavy.repair_pln > 25000
    assert heavy.risk > light.risk + 0.3
    assert "poduszki wystrzelone" in heavy.flags and any("brak zdjęć" in f for f in heavy.flags)
    assert heavy.breakdown["airbags"] == vision.AIRBAGS_SET and "adas_front" in heavy.breakdown


def test_segment_factor_scales_premium_cars():
    assert vision.estimate_repair(LIGHT, 140000).repair_pln > vision.estimate_repair(LIGHT, 40000).repair_pln


def test_parse_response_strips_fences_and_unknown_parts():
    raw = "```json\n" + json.dumps(dict(LIGHT, parts=LIGHT["parts"] + [{"part": "spoiler", "action": "replace"}])) + "\n```"
    d = vision.parse_response(raw)
    assert [p["part"] for p in d["parts"]] == ["rear_bumper", "taillight"]


def test_build_messages_contains_images_and_context():
    msgs = vision.build_messages({"title": "Kia Stinger GT", "make": "kia", "model": "stinger", "year": 2019,
                                  "mileage_km": 90000, "description": "Uszkodzony tył"}, ["u1", "u2"])
    user = msgs[1]["content"]
    assert "Kia Stinger GT" in user[0]["text"] and "Uszkodzony tył" in user[0]["text"]
    assert [c["image_url"]["url"] for c in user[1:]] == ["u1", "u2"]


def test_vision_overrides_keyword_heuristic():
    listing = {"title": "BMW", "description": "Wystrzelone poduszki", "vision": LIGHT}
    d = assess_damage(listing, 70000)
    assert d.source == "vision" and d.repair_pln and d.notes.startswith("Uszkodzony")
    err = assess_damage({"title": "BMW", "description": "lekko obtarty", "vision": {"_error": "x"}}, 70000)
    assert err.source == "keywords"


def test_evaluate_uses_vision_amount():
    listing = {"price_pln": 30000, "power_hp": 250, "gearbox": "automatic", "title": "Audi A6",
               "description": "Jedzie z USA", "vision": LIGHT}
    ev = evaluate(listing, Valuation(70000, "t", 20), CFG.criteria, CFG.scoring)
    est = vision.estimate_repair(LIGHT, 70000)
    assert ev.repair_estimate == est.repair_pln
    assert "import z USA" in ev.details["damage"]["flags"]  # ryzyko kontekstowe nadal działa


def test_analyze_calls_runpod_openai_route(monkeypatch):
    captured = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(LIGHT)}}],
                    "usage": {"prompt_tokens": 9000, "completion_tokens": 300}}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured.update(url=url, body=json, headers=headers)
        return Resp()

    monkeypatch.setattr(vision.creq, "post", fake_post)
    monkeypatch.setattr(vision, "fetch_image_data_url", lambda u: "data:image/jpeg;base64,QUJD")
    cfg = vision.VisionConfig(api_key="k", endpoint_id="abc123", model="Qwen/Qwen3-VL-8B-Instruct-FP8")
    res = vision.analyze(cfg, {"title": "x", "photos": ["https://cdn/a/image;s=1280x960"]})
    assert captured["url"] == "https://api.runpod.ai/v2/abc123/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert res["parts"][0]["part"] == "rear_bumper" and res["_meta"]["photos"] == 1
    img = captured["body"]["messages"][1]["content"][1]["image_url"]["url"]
    assert img.startswith("data:image/jpeg;base64,")
    assert vision.analyze(cfg, {"title": "x", "photos": []}) == {"_error": "no_photos"}


def test_retry_on_502(monkeypatch):
    calls = []

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, "bad gateway"

        def json(self):
            return {"ok": 1}

    def fake_post(*a, **k):
        calls.append(1)
        return R(502 if len(calls) < 2 else 200)

    monkeypatch.setattr(vision.creq, "post", fake_post)
    monkeypatch.setattr(vision.time, "sleep", lambda s: None)
    cfg = vision.VisionConfig(api_key="k", endpoint_id="e", model="m")
    assert vision._post_with_retry(cfg, {}) == {"ok": 1} and len(calls) == 2


def test_parse_truncated_and_repeated():
    looped = dict(LIGHT, parts=[{"part": "front_bumper", "action": "replace"}] * 40,
                  damage_zones=[{"zone": "front", "severity": "heavy"}] * 5)
    raw = json.dumps(looped)
    d = vision.parse_response(raw[: len(raw) - 40])     # ucięte w środku
    assert d["_repaired"]
    full = vision.parse_response(raw)
    assert len(full["parts"]) == 2 and len(full["damage_zones"]) == 1


def test_mechanical_issues_from_description():
    v = dict(LIGHT, mechanical_issues=["automatic_gearbox", "automatic_gearbox"])
    e = vision.estimate_repair(v, 70000)
    base = vision.estimate_repair(LIGHT, 70000)
    assert e.breakdown["mech:automatic_gearbox"] == vision.MECHANICAL_COSTS["automatic_gearbox"]
    assert e.repair_pln > base.repair_pln + 7000 and e.risk > base.risk
    assert any("usterki z opisu" in f for f in e.flags)


def test_airbags_force_at_least_moderate():
    e = vision.estimate_repair(dict(LIGHT, airbags_deployed="yes", overall_severity="light"), 70000)
    assert e.severity == "medium"


def test_sanitize_gearbox_and_under_car_parts():
    v = dict(LIGHT, parts=LIGHT["parts"] + [{"part": "steering_rack", "action": "replace"}],
             mechanical_issues=["manual_gearbox_clutch"])
    out = vision.sanitize(json.loads(json.dumps(v)), {"gearbox": "automatic"})
    assert out["mechanical_issues"] == ["automatic_gearbox"]
    assert all(p["part"] != "steering_rack" for p in out["parts"])
    kept = vision.sanitize(dict(json.loads(json.dumps(v)), suspension_damage="yes"), {"gearbox": "manual"})
    assert any(p["part"] == "steering_rack" for p in kept["parts"])
