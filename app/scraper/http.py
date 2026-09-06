"""Polite HTTP client shared by the scrapers.

Browser User-Agent (Metacritic 403s the default httpx one), a hard timeout, retries
with exponential backoff, and a global minimum delay between outgoing requests.
"""

from __future__ import annotations

import logging
import random
import threading
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)

#: Statuses worth retrying: transient server errors and rate limiting.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class ScrapeError(RuntimeError):
    """A request failed after all retries."""


class PoliteClient:
    """Thin wrapper over `httpx.Client` that never fires faster than `min_delay`."""

    def __init__(
        self,
        *,
        min_delay: float | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.min_delay = settings.request_delay if min_delay is None else min_delay
        self.retries = settings.request_retries if retries is None else retries
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=settings.request_timeout if timeout is None else timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent or settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def _wait_turn(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last_request_at
            if gap < self.min_delay:
                time.sleep(self.min_delay - gap)
            self._last_request_at = time.monotonic()

    def get(self, url: str, **kwargs) -> httpx.Response:
        """GET with retries. Raises :class:`ScrapeError` when every attempt fails."""
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._wait_turn()
            try:
                response = self._client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("GET %s failed (attempt %d/%d): %s", url, attempt, self.retries, exc)
            else:
                if response.status_code not in RETRY_STATUSES:
                    response.raise_for_status()
                    return response
                last_error = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                log.warning(
                    "GET %s returned %d (attempt %d/%d)",
                    url,
                    response.status_code,
                    attempt,
                    self.retries,
                )
            if attempt < self.retries:
                # Exponential backoff with jitter, so parallel workers do not sync up.
                time.sleep(2 ** (attempt - 1) + random.uniform(0, 0.5))
        raise ScrapeError(f"GET {url} failed after {self.retries} attempts: {last_error}")

    def get_text(self, url: str, **kwargs) -> str:
        return self.get(url, **kwargs).text

    def get_json(self, url: str, **kwargs) -> dict:
        return self.get(url, **kwargs).json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


_default_client: PoliteClient | None = None


def default_client() -> PoliteClient:
    """Process-wide client, so the rate limit applies across all call sites."""
    global _default_client
    if _default_client is None:
        _default_client = PoliteClient()
    return _default_client
