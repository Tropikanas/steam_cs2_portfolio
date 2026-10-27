import logging
import time
from typing import Any

import httpx

from steamfolio.market.types import MarketError

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Паузы перед повторами, секунды. На 429 Steam обычно отпускает за 1–2 минуты.
BACKOFF = (15, 45, 90, 180)


class SteamHttp:
    """HTTP-клиент для steamcommunity.com: пауза между запросами и повторы при отказе."""

    def __init__(self, delay: float, proxy: str | None = None, backoff: tuple[float, ...] = BACKOFF):
        self._delay = delay
        self._backoff = backoff
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=30,
            proxy=proxy,
        )

    def get_json(self, url: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        problem = ""
        for attempt in range(len(self._backoff) + 1):
            if attempt:
                pause = self._backoff[attempt - 1]
                log.warning("Steam: %s, повтор %d через %s с", problem, attempt, pause)
                time.sleep(pause)
            self._throttle()
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as e:
                problem = f"сетевая ошибка {e!r}"
                continue
            if response.status_code in RETRY_STATUSES:
                problem = f"HTTP {response.status_code}"
                continue
            if response.status_code != 200:
                raise MarketError(f"HTTP {response.status_code} для {response.url}")
            try:
                return response.json()
            except ValueError:
                problem = "ответ не JSON"
        raise MarketError(f"{problem} после {len(self._backoff)} повторов: {url}")

    def _throttle(self) -> None:
        wait = self._last_request + self._delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def close(self) -> None:
        self._client.close()
