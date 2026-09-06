"""Retry / rate-limit behaviour of the shared client, driven by a mock transport."""

import httpx
import pytest

from app.scraper.http import PoliteClient, ScrapeError


def _client_with(handler, **kwargs) -> PoliteClient:
    client = PoliteClient(min_delay=0.0, retries=3, backoff_base=0.01, **kwargs)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_sends_a_browser_user_agent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text="ok")

    client = PoliteClient(min_delay=0.0)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), headers=client._client.headers
    )
    assert client.get_text("https://example.test/") == "ok"
    assert "Mozilla/5.0" in seen["ua"]


def test_retries_transient_status_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="ok") if len(calls) == 3 else httpx.Response(503)

    assert _client_with(handler).get_text("https://example.test/") == "ok"
    assert len(calls) == 3


def test_gives_up_after_the_configured_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    with pytest.raises(ScrapeError):
        _client_with(handler).get("https://example.test/")
    assert len(calls) == 3


def test_network_errors_are_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(ScrapeError):
        _client_with(handler).get("https://example.test/")
    assert len(calls) == 3


def test_client_errors_are_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404)

    # Not retried, but still a ScrapeError: callers only ever catch one type.
    with pytest.raises(ScrapeError, match="404"):
        _client_with(handler).get("https://example.test/")
    assert len(calls) == 1


def test_credentials_in_a_url_never_reach_the_error_message():
    def handler(request):
        return httpx.Response(403)

    with pytest.raises(ScrapeError) as caught:
        _client_with(handler).get("https://example.test/x?apiKey=SECRET123&offset=0")
    message = str(caught.value)
    assert "SECRET123" not in message
    assert "apiKey=<redacted>" in message
    assert "offset=0" in message  # harmless parameters survive


def test_scrub_redacts_common_credential_parameters():
    from app.scraper.http import scrub

    assert scrub("https://a/b?token=xyz&q=1") == "https://a/b?token=<redacted>&q=1"
    assert scrub("https://a/b?ACCESS_TOKEN=xyz") == "https://a/b?ACCESS_TOKEN=<redacted>"
    assert scrub("nothing to hide") == "nothing to hide"


def test_min_delay_spaces_out_requests():
    client = _client_with(lambda request: httpx.Response(200, text="ok"))
    client.min_delay = 0.2
    import time

    started = time.monotonic()
    for _ in range(3):
        client.get("https://example.test/")
    # First call goes out immediately, the next two wait their turn.
    assert time.monotonic() - started >= 0.4
