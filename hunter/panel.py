"""Panel web: ranking + edycja ustawień. Uruchomienie: python -m hunter panel"""
from __future__ import annotations

import html
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import quote

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db, pipeline
from .config import load_config
from .notify import cepik_summary, fmt_pln
from .settings import GROUPS, FormError, _set, get_value, parse_form, save_config

log = logging.getLogger(__name__)
security = HTTPBasic()

FLAG_LABELS = {"light": "lekka", "medium": "średnia", "severe": "ciężka"}


def require_auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    user = os.environ.get("PANEL_USER", "admin")
    pwd = os.environ.get("PANEL_PASSWORD", "")
    if not pwd:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Ustaw PANEL_PASSWORD w .env")
    ok = secrets.compare_digest(creds.username.encode(), user.encode()) and \
        secrets.compare_digest(creds.password.encode(), pwd.encode())
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Złe dane", headers={"WWW-Authenticate": "Basic"})
    return creds.username


e = html.escape

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#667085;--line:#e4e7ec;--acc:#2563eb;--ok:#15803d;--bad:#b42318;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--fg:#e6e8ec;--muted:#98a2b3;--line:#2a303b;--acc:#60a5fa;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:16px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
header b{font-size:17px}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--fg);font-weight:600}
main{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:baseline}.muted{color:var(--muted)}.sm{font-size:13px}
.price{font-weight:700;font-size:17px}.score{font-weight:700;color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.chips span{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:1px 8px;margin:2px 4px 0 0;font-size:12px}
.chips span.w{border-color:var(--warn);color:var(--warn)}
a{color:var(--acc)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:12px}
.stats div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.stats b{display:block;font-size:20px}
fieldset{border:1px solid var(--line);border-radius:10px;background:var(--card);margin:0 0 12px;padding:10px 16px 14px}
legend{font-weight:700;padding:0 6px}
.f{display:grid;grid-template-columns:minmax(150px,240px) 1fr;gap:4px 14px;align-items:start;padding:8px 0;border-bottom:1px dashed var(--line)}
.f:last-child{border-bottom:0}.f label.l{font-weight:500;padding-top:6px}
input[type=text],input[type=number],select{width:100%;max-width:340px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
.opts{display:flex;flex-wrap:wrap;gap:4px 14px;padding-top:6px}.opts label{white-space:nowrap}
.help{grid-column:2;font-size:12px;color:var(--muted)}.err{grid-column:2;font-size:13px;color:var(--bad);font-weight:600}
button{font:inherit;padding:9px 16px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
button.sec{background:transparent;color:var(--acc)}
.bar{position:sticky;bottom:0;background:var(--bg);padding:10px 0;display:flex;gap:10px;flex-wrap:wrap}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:12px;border:1px solid var(--ok);color:var(--ok)}.msg.e{border-color:var(--bad);color:var(--bad)}
@media (max-width:640px){.f{grid-template-columns:1fr}.help,.err{grid-column:1}header{gap:12px}}
"""


def page(title: str, active: str, body: str) -> HTMLResponse:
    nav = "".join(f'<a href="{u}" class="{"on" if k == active else ""}">{t}</a>'
                  for k, u, t in [("top", "/", "Ranking"), ("cfg", "/config", "Ustawienia")])
    return HTMLResponse(f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · car-hunter</title>
<style>{CSS}</style></head><body><header><b>🚗 car-hunter</b>{nav}</header><main>{body}</main></body></html>""")


def create_app() -> FastAPI:
    app = FastAPI(title="car-hunter", docs_url=None, redoc_url=None, openapi_url=None)
    config_path = os.environ.get("HUNTER_CONFIG") or "config.yaml"

    def conn():
        c = db.connect(load_config(config_path).database_url)
        try:
            yield c
        finally:
            c.close()

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def ranking(request: Request, n: int = 30, msg: str = "", gems: int = 0, _=Depends(require_auth),
                c=Depends(conn)):
        rows = pipeline.top(c, min(max(n, 5), 200), gems_only=bool(gems))
        st = c.execute("""SELECT
              (SELECT count(*) FROM listings WHERE kind='candidate' AND active) AS cand,
              (SELECT count(*) FROM evaluations e JOIN listings l ON l.id=e.listing_id WHERE l.active AND e.passes_filters) AS passing,
              (SELECT count(*) FROM listings WHERE vision_at > now() - interval '1 day') AS vision24,
              (SELECT count(*) FROM evaluations e JOIN listings l ON l.id=e.listing_id
                 WHERE l.active AND (e.details->>'gem')::boolean) AS gems,
              (SELECT count(*) FROM alerted WHERE alerted_at > now() - interval '7 days') AS alerts7,
              (SELECT count(*) FROM listings WHERE kind='market' AND active) AS market""").fetchone()
        kv = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM kv").fetchall()}

        def ago(k):
            v = kv.get(k)
            if not v:
                return "—"
            try:
                d = datetime.now(timezone.utc) - datetime.fromisoformat(v)
                m = int(d.total_seconds() // 60)
                return f"{m} min temu" if m < 120 else f"{m // 60} h temu"
            except ValueError:
                return "—"

        out = [f'<div class="msg">{e(msg)}</div>' if msg else ""]
        out.append(f"""<div class="stats">
<div><span class="muted sm">Uszkodzone aktywne</span><b>{st['cand']}</b></div>
<div><span class="muted sm">Spełnia kryteria</span><b>{st['passing']}</b></div>
<div><span class="muted sm">💎 Perełki teraz</span><b>{st['gems']}</b></div>
<div><span class="muted sm">Alerty / 7 dni</span><b>{st['alerts7']}</b></div>
<div><span class="muted sm">Zdrowe (wycena)</span><b>{st['market']}</b></div>
<div><span class="muted sm">Ocen zdjęć / 24 h</span><b>{st['vision24']}</b></div>
<div><span class="muted sm">Rynek odświeżony</span><b class="sm">{ago('last_market')}</b></div>
<div><span class="muted sm">Ostatni pakiet zdjęć</span><b class="sm">{ago('last_vision')}</b></div></div>
<form method="post" action="/evaluate" class="row" style="margin-bottom:12px"><button class="sec">Przelicz ranking teraz</button>
<a href="/?gems={0 if gems else 1}">{'pokaż wszystkie' if gems else 'tylko perełki'}</a>
<span class="muted sm">jedno auto = jeden wiersz, nawet gdy wisi w kilku ogłoszeniach</span></form>""")
        if not rows:
            out.append('<div class="card">Brak aut spełniających kryteria. Poluzuj ustawienia albo poczekaj na kolejny cykl.</div>')
        for i, r in enumerate(rows, 1):
            d = r.get("details") or {}
            dmg = d.get("damage") or {}
            vis = dmg.get("source") == "vision"
            spec = " · ".join(str(x) for x in [
                r.get("year"), f"{r['mileage_km']:,} km".replace(",", " ") if r.get("mileage_km") else None,
                f"{r['power_hp']} KM" if r.get("power_hp") else None, r.get("fuel"), r.get("body_type"), r.get("city")] if x)
            chips = "".join(f'<span class="w">{e(f)}</span>' for f in dmg.get("flags") or [])
            chips += "".join(f"<span>{e(x)}</span>" for x in d.get("reasons") or [] if x.startswith("+"))
            sev = FLAG_LABELS.get(dmg.get("severity"), dmg.get("severity") or "?")
            gem = d.get("gem")
            miss = d.get("gem_missing") or []
            gem_line = ('<div class="sm score">💎 Perełka - spełnia wszystkie warunki</div>' if gem else
                        f'<div class="sm muted">nie perełka: {e("; ".join(miss[:3]))}</div>' if miss else "")
            out.append(f"""<div class="card"{' style="border-color:var(--ok);border-width:2px"' if gem else ''}><div class="row"><span class="muted">#{i}</span>
<a href="{e(r['url'] or '#')}" target="_blank" rel="noopener"><b>{e(r['title'] or '')}</b></a>
<span class="muted sm">{e(spec)}</span></div>
<div class="row" style="margin-top:6px"><span class="price">{fmt_pln(r['price_pln'])}</span>
<span>rynek {fmt_pln(r['market_value'])} <span class="muted sm">({r['comparables_n']} por.)</span></span>
<span>naprawa {fmt_pln(r['repair_estimate'])} <span class="muted sm">({'ze zdjęć' if vis else 'szacunek'}, {e(sev)})</span></span>
<span>oszczędność <b>{fmt_pln(r['savings'])}</b></span>
<span class="{'bad' if float(r['risk'] or 0) >= .4 else 'warn' if float(r['risk'] or 0) >= .25 else 'muted'}">ryzyko {float(r['risk'] or 0):.0%}</span>
<span class="score">score {fmt_pln(r['score'])}</span>{' <span class="muted sm">✓ alert</span>' if r.get('alerted_at') else ''}</div>
{f'<div class="chips">{chips}</div>' if chips else ''}
{f'<div class="sm" style="margin-top:6px">🔍 {e(dmg["notes"])}</div>' if dmg.get("notes") else ''}
{f'<div class="sm" style="margin-top:4px">📋 CEPiK: {e(cepik_summary(r["cepik"]))}</div>' if r.get("cepik") else ''}
{f'<div class="sm muted">VIN {e(r["vin"])} · <a target="_blank" rel="noopener" href="https://www.google.com/search?q={quote(chr(34) + r["vin"] + chr(34))}">historia aukcji</a> · <a target="_blank" rel="noopener" href="https://historiapojazdu.gov.pl/">historiapojazdu.gov.pl</a></div>' if r.get("vin") else ''}
{gem_line}
{'' if vis else f'<form method="post" action="/vision/{r["id"]}" style="margin-top:8px"><button class="sec">Oceń zdjęcia (~$0,01)</button></form>'}
</div>""")
        return page("Ranking", "top", "".join(out))

    @app.post("/evaluate")
    def evaluate_now(_=Depends(require_auth), c=Depends(conn)):
        stats = pipeline.evaluate_all(load_config(config_path), c)
        return RedirectResponse(f"/?msg={quote('Przeliczono: ' + str(stats.get('passing', 0)) + ' aut spełnia kryteria')}",
                                status_code=303)

    @app.post("/vision/{listing_id}")
    def vision_one(listing_id: int, _=Depends(require_auth), c=Depends(conn)):
        cfg = load_config(config_path)
        res = pipeline.run_vision(cfg, c, listing_id=listing_id)
        pipeline.evaluate_all(cfg, c)
        msg = "Ocena zdjęć gotowa" if res.get("ok") else f"Ocena nieudana: {res}"
        return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)

    def render_form(data: dict, errors: dict | None = None, msg: str = "") -> HTMLResponse:
        errors = errors or {}
        parts = []
        if msg:
            parts.append(f'<div class="msg">{e(msg)}</div>')
        if errors:
            parts.append('<div class="msg e">Popraw zaznaczone pola - nic nie zapisano.</div>')
        parts.append('<form method="post" action="/config">')
        for _, title, fields in GROUPS:
            parts.append(f"<fieldset><legend>{e(title)}</legend>")
            for f in fields:
                v = get_value(data, f)
                n = e(f.name)
                if f.kind == "bool":
                    ctrl = f'<div class="opts"><label><input type="checkbox" name="{n}" {"checked" if v else ""}> tak</label></div>'
                elif f.kind == "multi":
                    cur = set(v or [])
                    ctrl = '<div class="opts">' + "".join(
                        f'<label><input type="checkbox" name="{n}" value="{e(o)}" {"checked" if o in cur else ""}> {e(o)}</label>'
                        for o in f.options) + "</div>"
                elif f.kind == "choice":
                    ctrl = f'<select name="{n}">' + "".join(
                        f'<option value="{e(o)}" {"selected" if (v or "") == o else ""}>{e(o or "dowolna")}</option>'
                        for o in f.options) + "</select>"
                elif f.kind == "list":
                    ctrl = f'<input type="text" name="{n}" value="{e(", ".join(v or []))}">'
                else:
                    step = "1" if f.kind == "int" else "any"
                    ctrl = f'<input type="number" step="{step}" name="{n}" value="{"" if v is None else e(str(v))}">'
                parts.append(f'<div class="f"><label class="l">{e(f.label)}</label>{ctrl}'
                             + (f'<div class="err">{e(errors[f.name])}</div>' if f.name in errors else "")
                             + (f'<div class="help">{e(f.help)}</div>' if f.help else "") + "</div>")
            parts.append("</fieldset>")
        parts.append('<div class="bar"><button>Zapisz</button><button class="sec" name="and_eval" value="1">'
                     'Zapisz i przelicz ranking</button><a href="/config" style="align-self:center">Anuluj</a></div></form>')
        parts.append('<p class="muted sm">Zapis tworzy kopię w config-backups/. Pętla bierze nowe ustawienia w następnym '
                     'cyklu (do 30 min); kryteria pobierania (cena, rocznik) w pełni po najbliższym pełnym przebiegu.</p>')
        return page("Ustawienia", "cfg", "".join(parts))

    @app.get("/config", response_class=HTMLResponse)
    def config_form(msg: str = "", _=Depends(require_auth)):
        return render_form(load_config(config_path).data, msg=msg)

    @app.post("/config", response_class=HTMLResponse)
    async def config_save(request: Request, _=Depends(require_auth)):
        form = await request.form()
        data: dict = {}
        for k in form.keys():
            vals = form.getlist(k)
            data[k] = vals if len(vals) > 1 else vals[0]
        # pola multi z jednym zaznaczeniem też jako lista
        for _, _, fields in GROUPS:
            for f in fields:
                if f.kind == "multi" and f.name in data and not isinstance(data[f.name], list):
                    data[f.name] = [data[f.name]]
        try:
            changes = parse_form(data)
        except FormError as err:
            current = load_config(config_path).data
            # pokaż to, co użytkownik wpisał
            shown = {s: dict(v) for s, v in current.items() if isinstance(v, dict)}
            for _, _, fields in GROUPS:
                for f in fields:
                    raw = data.get(f.name)
                    if f.kind in ("int", "float", "list") and raw is not None:
                        _set(shown, f.section, f.key, raw if f.kind != "list" else
                             [x.strip() for x in str(raw).split(",") if x.strip()])
            return render_form(shown, err.errors)
        save_config(config_path, changes)
        log.info("config saved via panel")
        msg = "Zapisano."
        if data.get("and_eval"):
            c = db.connect(load_config(config_path).database_url)
            try:
                stats = pipeline.evaluate_all(load_config(config_path), c)
            finally:
                c.close()
            passing = stats.get("passing", 0)
            return RedirectResponse("/?msg=" + quote(f"Zapisano. Spełnia kryteria: {passing}"), status_code=303)
        return RedirectResponse(f"/config?msg={quote(msg)}", status_code=303)

    @app.get("/config.yaml", include_in_schema=False)
    def raw_yaml(_=Depends(require_auth)):
        return HTMLResponse("<pre>" + e(yaml.safe_dump(load_config(config_path).data, allow_unicode=True,
                                                       sort_keys=False)) + "</pre>")

    return app


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn
    uvicorn.run(create_app(), host=host, port=port, log_level="info", proxy_headers=True)
