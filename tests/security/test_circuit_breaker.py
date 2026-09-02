"""Devre kesici davranisi.

Rehber Madde 11: "Devre kesici kosullari" tanimli olmali. Test edilen sozlesme:

  1. Ardisik altyapi hatasi esigi asinca devre acilir.
  2. Devre acikken istek SAP'a **gonderilmez** (yan etki yok).
  3. Is/yetki hatalari (401/403/404/409) devreyi ACMAZ.
  4. Bekleme suresi sonunda tek deneme cagrisi gecer; basarirsa devre kapanir.
  5. Deneme cagrisi da hata verirse devre yeniden acilir.
  6. Yazma yolunda kesici belirsizlik URETMEZ: istek gonderilmedigi icin
     mutabakat gerekmez.
"""

from __future__ import annotations

import httpx
import pytest

from robotics_agent.adapters.sap import CircuitBreaker, CircuitOpen, ODataHttpCore, breaker_for
from robotics_agent.adapters.sap.errors import SAPError


class FakeClock:
    """Testin ilerlettigi monotonic saat."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(clock: FakeClock, *, threshold: int = 3, reset: float = 30.0) -> CircuitBreaker:
    return CircuitBreaker(
        name="S4Q", failure_threshold=threshold, reset_seconds=reset, clock=clock
    )


def make_core(breaker: CircuitBreaker, handler) -> ODataHttpCore:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://s4hana.example")
    return ODataHttpCore(
        client=client,
        odata_version="v4",
        allowed_hosts=("s4hana.example",),
        max_retries=1,
        csrf_enabled=False,
        read_only=False,
        breaker=breaker,
        sleep=lambda _s: None,
    )


# --- Birim davranisi --------------------------------------------------------


def test_breaker_opens_after_threshold():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=3)

    for _ in range(2):
        breaker.allow()
        breaker.record_failure()
    assert breaker.state == "closed", "esik altinda devre kapali kalmali"

    breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open"

    with pytest.raises(CircuitOpen) as exc:
        breaker.allow()
    assert exc.value.request_sent is False
    assert exc.value.code == "CIRCUIT_OPEN"


def test_success_resets_failure_counter():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=3)

    breaker.allow()
    breaker.record_failure()
    breaker.allow()
    breaker.record_success()
    breaker.allow()
    breaker.record_failure()
    breaker.allow()
    breaker.record_failure()

    assert breaker.state == "closed", "araya giren basari sayaci sifirlamali"


def test_half_open_probe_closes_circuit_on_success():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=2, reset=30.0)

    for _ in range(2):
        breaker.allow()
        breaker.record_failure()
    assert breaker.state == "open"

    clock.advance(31.0)
    breaker.allow()  # deneme cagrisi gecer
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "closed"


def test_half_open_probe_failure_reopens_immediately():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=2, reset=30.0)

    for _ in range(2):
        breaker.allow()
        breaker.record_failure()

    clock.advance(31.0)
    breaker.allow()
    breaker.record_failure()

    assert breaker.state == "open"
    # Bekleme suresi bastan baslar.
    clock.advance(1.0)
    with pytest.raises(CircuitOpen):
        breaker.allow()


def test_half_open_allows_only_one_probe():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=10.0)
    breaker.allow()
    breaker.record_failure()

    clock.advance(11.0)
    breaker.allow()  # ilk deneme
    with pytest.raises(CircuitOpen):
        breaker.allow()  # ikinci es zamanli deneme reddedilir


def test_business_status_is_not_infrastructure_failure():
    for status in (400, 401, 403, 404, 409, 412):
        assert CircuitBreaker.counts_as_failure(status) is False, status
    for status in (429, 500, 502, 503, 504):
        assert CircuitBreaker.counts_as_failure(status) is True, status


def test_disabled_breaker_never_blocks():
    clock = FakeClock()
    breaker = CircuitBreaker(name="off", enabled=False, failure_threshold=1, clock=clock)
    for _ in range(10):
        breaker.allow()
        breaker.record_failure()
    assert breaker.state == "closed"


# --- HTTP cekirdegiyle entegrasyon ------------------------------------------


def test_core_opens_circuit_on_repeated_5xx():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=2)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"message": {"value": "yogunluk"}}})

    core = make_core(breaker, handler)

    for _ in range(2):
        with pytest.raises(SAPError):
            core.request("GET", "/svc/Entity")
    assert breaker.state == "open"

    sent_before = calls["n"]
    with pytest.raises(CircuitOpen):
        core.request("GET", "/svc/Entity")
    assert calls["n"] == sent_before, "devre acikken istek SAP'a gonderilmemeli"


def test_core_does_not_open_circuit_on_authorization_error():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": {"value": "yetkisiz"}}})

    core = make_core(breaker, handler)
    for _ in range(5):
        with pytest.raises(SAPError):
            core.request("GET", "/svc/Entity")

    assert breaker.state == "closed", "yetki hatasi SAP'in sagligini olcmez"


def test_core_opens_circuit_on_repeated_timeouts():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=2)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    core = make_core(breaker, handler)
    for _ in range(2):
        with pytest.raises(httpx.TimeoutException):
            core.request("GET", "/svc/Entity")

    assert breaker.state == "open"


def test_write_is_blocked_before_request_leaves():
    """Devre acikken yazma istegi gonderilmez; mutabakat gerekmez."""
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(503, json={"error": {"message": {"value": "kapali"}}})

    core = make_core(breaker, handler)
    with pytest.raises(SAPError):
        core.request("GET", "/svc/Entity")
    assert breaker.state == "open"

    with pytest.raises(CircuitOpen) as exc:
        core.request("POST", "/svc/Entity", json_body={"a": 1})

    assert "POST" not in seen, "yazma istegi devre acikken cikmamali"
    assert exc.value.request_sent is False


def test_successful_call_recovers_after_reset_window():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=2, reset=30.0)
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(502, json={"error": {"message": {"value": "gateway"}}})
        return httpx.Response(200, json={"value": [{"Product": "M-1"}]})

    core = make_core(breaker, handler)
    for _ in range(2):
        with pytest.raises(SAPError):
            core.request("GET", "/svc/Entity")
    assert breaker.state == "open"

    clock.advance(31.0)
    state["fail"] = False
    response = core.request("GET", "/svc/Entity")

    assert response.status_code == 200
    assert breaker.state == "closed"


def test_breaker_telemetry_counts_short_circuited_calls():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1)
    breaker.allow()
    breaker.record_failure()

    for _ in range(3):
        with pytest.raises(CircuitOpen):
            breaker.allow()

    snapshot = breaker.to_dict()
    assert snapshot["state"] == "open"
    assert snapshot["short_circuited_calls"] == 3
    assert snapshot["opened_count"] == 1


def test_breaker_from_settings(settings):
    object.__setattr__(settings.sap, "breaker_failure_threshold", 7)
    object.__setattr__(settings.sap, "breaker_reset_seconds", 12.5)
    breaker = breaker_for(settings.sap)

    assert breaker.failure_threshold == 7
    assert breaker.reset_seconds == 12.5
    assert breaker.name == settings.sap.system_alias
