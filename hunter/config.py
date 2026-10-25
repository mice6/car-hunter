from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    data: dict[str, Any]
    database_url: str
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    runpod_api_key: str | None = None
    runpod_endpoint_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # wygodne skróty
    @property
    def criteria(self) -> dict:
        return self.data["criteria"]

    @property
    def preferences(self) -> dict:
        return self.data.get("preferences") or {}

    @property
    def scoring(self) -> dict:
        return self.data["scoring"]

    @property
    def scraper(self) -> dict:
        return self.data["scraper"]

    @property
    def vision(self) -> dict:
        return self.data.get("vision") or {}

    @property
    def vision_enabled(self) -> bool:
        return bool(self.runpod_api_key and self.runpod_endpoint_id)

    @property
    def vision_auto(self) -> bool:
        return self.vision_enabled and bool(self.vision.get("auto", False))

    @property
    def schedule(self) -> dict:
        return self.data["schedule"]


def load_config(path: str | None = None) -> Config:
    root = Path(__file__).resolve().parent.parent
    defaults = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))
    path = path or os.environ.get("HUNTER_CONFIG") or str(root / "config.yaml")
    data = defaults
    if Path(path).exists():
        user = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        data = _deep_merge(defaults, user)
    return Config(
        data=data,
        database_url=os.environ.get("DATABASE_URL", "postgresql://hunter:hunter@localhost:5432/hunter"),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        runpod_api_key=os.environ.get("RUNPOD_API_KEY") or None,
        runpod_endpoint_id=os.environ.get("RUNPOD_ENDPOINT_ID") or None,
        extra={"path": path},
    )
