import os

import pytest
import yaml

from hunter.settings import ALL_FIELDS, FormError, parse_form, save_config

BASE_FORM = {
    "criteria.min_power_hp": "170", "criteria.gearbox": "automatic", "criteria.min_year": "2017",
    "criteria.max_year": "", "criteria.max_mileage_km": "200 000", "criteria.fuels": ["diesel", "petrol"],
    "criteria.include_makes": "Kia, Volvo", "criteria.exclude_makes": "", "criteria.include_models": "",
    "criteria.exclude_models": "ford/ranger", "criteria.budget_total_pln": "55000",
    "criteria.max_purchase_pln": "45000", "criteria.min_price_ratio": "0,35", "criteria.max_damage": "moderate",
    "criteria.exclude_airbags": "on", "criteria.min_photos": "5", "scoring.pref_bonus_pln": "3000",
    "scoring.min_score_alert_pln": "10000", "vision.every_hours": "3", "vision.max_per_run": "25",
    "vision.max_per_day": "150", "vision.auto": "on", "scoring.implied_damage_share": "0.5",
    "scoring.vision_min_gap_share": "0.25", "scoring.asking_to_transaction": "0.93",
    "scoring.valuation_year_window": "2", "schedule.candidates_every_min": "30",
    "alerts.gems_only": "on", "alerts.max_per_cycle": "3", "scoring.gem.min_savings_pln": "15000",
    "scoring.gem.min_ratio": "0.5", "scoring.gem.max_ratio": "0.85", "scoring.gem.min_comparables": "10",
    "scoring.gem.max_risk": "0.3", "scoring.gem.require_drivable": "on", "vision.mode": "gems", "vision.top_n": "5",
}


def test_parse_form_types():
    out = parse_form(BASE_FORM)
    c = out["criteria"]
    assert c["max_mileage_km"] == 200000 and c["max_year"] is None and c["min_price_ratio"] == 0.35
    assert c["fuels"] == ["diesel", "petrol"] and c["bodies"] == [] and c["include_makes"] == ["kia", "volvo"]
    assert c["exclude_airbags"] is True and c["exclude_structural"] is False   # brak checkboxa = False
    assert out["vision"]["auto"] is True and out["vision"]["require_for_alert"] is False
    assert {f.section for f in ALL_FIELDS} == set(out)
    assert out["scoring"]["gem"]["min_savings_pln"] == 15000 and out["alerts"]["gems_only"] is True


def test_parse_form_errors():
    with pytest.raises(FormError) as ei:
        parse_form(dict(BASE_FORM, **{"criteria.min_year": "abc", "criteria.max_year": "2010",
                                      "criteria.include_models": "kia", "criteria.min_price_ratio": "2"}))
    assert "criteria.min_year" in ei.value.errors and "criteria.min_price_ratio" in ei.value.errors
    with pytest.raises(FormError) as ei:
        parse_form(dict(BASE_FORM, **{"criteria.max_purchase_pln": "90000"}))
    assert "criteria.max_purchase_pln" in ei.value.errors


def test_save_config_merges_and_backs_up(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("criteria:\n  min_year: 2010\n  custom_key: 7\nvision:\n  model: qwen/x\n"
                 "scoring:\n  gem:\n    light_repair_share: 0.1\n", encoding="utf-8")
    save_config(p, parse_form(BASE_FORM))
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["criteria"]["min_year"] == 2017 and data["criteria"]["custom_key"] == 7
    assert data["vision"]["model"] == "qwen/x" and data["vision"]["auto"] is True
    assert len(list((tmp_path / "config-backups").glob("config-*.yaml"))) == 1
    assert data["scoring"]["gem"]["light_repair_share"] == 0.1 and data["scoring"]["gem"]["min_ratio"] == 0.5


DSN = os.environ.get("HUNTER_TEST_DB")


@pytest.mark.skipif(not DSN, reason="HUNTER_TEST_DB not set")
def test_panel_http(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from hunter import db
    from hunter.panel import create_app
    c = db.connect(DSN)
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init_db(c)
    c.close()
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("vision:\n  model: qwen/x\n", encoding="utf-8")
    monkeypatch.setenv("HUNTER_CONFIG", str(cfgp))
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("PANEL_PASSWORD", "s3cret")
    client = TestClient(create_app())
    assert client.get("/").status_code == 401
    auth = ("admin", "s3cret")
    r = client.get("/", auth=auth)
    assert r.status_code == 200 and "Brak aut" in r.text
    r = client.get("/config", auth=auth)
    assert r.status_code == 200 and 'name="criteria.min_year"' in r.text
    r = client.post("/config", data=BASE_FORM, auth=auth, follow_redirects=False)
    assert r.status_code == 303
    assert yaml.safe_load(cfgp.read_text(encoding="utf-8"))["criteria"]["min_year"] == 2017
    bad = dict(BASE_FORM, **{"criteria.min_year": "x"})
    r = client.post("/config", data=bad, auth=auth)
    assert r.status_code == 200 and "nic nie zapisano" in r.text
    assert yaml.safe_load(cfgp.read_text(encoding="utf-8"))["criteria"]["min_year"] == 2017
    r = client.post("/evaluate", auth=auth, follow_redirects=False)
    assert r.status_code == 303
