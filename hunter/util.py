from __future__ import annotations

import html as _html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def html_to_text(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", s or "")
    s = _html.unescape(_TAG.sub(" ", s))
    s = _WS.sub(" ", s)
    return _NL.sub("\n", s).strip()


def to_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else None


BODY_MAP = {
    "combi": "combi", "kombi": "combi", "estate-car": "combi", "estate": "combi",
    "suv": "suv", "terenowy": "suv", "suv-crossover": "suv",
    "sedan": "sedan", "limuzyna": "sedan",
    "compact": "compact", "hatchback": "compact",
    "minivan": "minivan", "van": "minivan",
    "coupe": "coupe", "cabrio": "cabrio", "kabriolet": "cabrio",
    "city-car": "city-car", "small-car": "city-car",
}

FUEL_MAP = {
    "diesel": "diesel", "petrol": "petrol", "benzyna": "petrol",
    "petrol-lpg": "petrol-lpg", "lpg": "petrol-lpg",
    "hybrid": "hybrid", "hybryda": "hybrid", "plugin-hybrid": "plugin-hybrid",
    "electric": "electric", "elektryczny": "electric",
}


def norm_body(v: str | None) -> str | None:
    if not v:
        return None
    return BODY_MAP.get(v.lower(), v.lower())


def norm_fuel(v: str | None) -> str | None:
    if not v:
        return None
    return FUEL_MAP.get(v.lower(), v.lower())


def slug(v: str | None) -> str | None:
    if not v:
        return None
    s = v.lower().strip()
    s = (s.replace("ą", "a").replace("ć", "c").replace("ę", "e").replace("ł", "l").replace("ń", "n")
         .replace("ó", "o").replace("ś", "s").replace("ź", "z").replace("ż", "z"))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")
