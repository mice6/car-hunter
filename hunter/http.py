from __future__ import annotations

import logging
import random
import time
from urllib.parse import urlparse

from curl_cffi import requests as creq

log = logging.getLogger(__name__)

RETRY_STATUS = {403, 429, 500, 502, 503, 504}


class BlockedError(RuntimeError):
    pass


class Http:
    """Sesja HTTP z odciskiem Chrome, losowym opóźnieniem per host i backoffem."""

    def __init__(self, delay_min: float = 2.5, delay_max: float = 6.0, max_retries: int = 4):
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_retries = max_retries
        self.session = creq.Session(impersonate="chrome")
        self.session.headers.update({"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6"})
        self._last: dict[str, float] = {}
        self.requests_made = 0

    def _throttle(self, host: str) -> None:
        wait = random.uniform(self.delay_min, self.delay_max)
        elapsed = time.monotonic() - self._last.get(host, 0)
        if elapsed < wait:
            time.sleep(wait - elapsed)
        self._last[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, *, json: bool = False, headers: dict | None = None):
        host = urlparse(url).netloc
        for attempt in range(1, self.max_retries + 1):
            self._throttle(host)
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=30)
                self.requests_made += 1
            except Exception as e:  # sieć
                log.warning("GET %s failed (%s), attempt %d", url, e, attempt)
                time.sleep(10 * attempt)
                continue
            if r.status_code == 200:
                return r.json() if json else r.text
            if r.status_code == 404:
                return None
            if r.status_code in RETRY_STATUS:
                backoff = 30 * attempt + random.uniform(0, 15)
                log.warning("GET %s -> %d, backoff %.0fs (attempt %d)", url, r.status_code, backoff, attempt)
                time.sleep(backoff)
                continue
            log.error("GET %s -> %d", url, r.status_code)
            return None
        raise BlockedError(f"Giving up on {url} after {self.max_retries} attempts")
