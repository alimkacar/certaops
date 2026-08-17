"""BTP Destination token yenileme ve Cloud Connector yonlendirme testleri.

Iki ariza sinifini kapsar:

1. Destination token'i bir kez alinip client header'ina gomulurse, uzun omurlu
   bir serviste sure dolunca her SAP cagrisi 401 doner. Ilk saatler sorunsuz
   gectigi icin teshisi zordur.
2. `ProxyType: OnPremise` bir bilgi etiketi degil yonlendirme karari; proxy
   uygulanmazsa istek ic aga hic ulasmaz ve timeout'a duser.
"""

from __future__ import annotations

import httpx
import pytest

from robotics_agent.adapters.sap.destination import (
    DestinationResolver,
    DestinationTokenProvider,
    OAuth2TokenProvider,
    build_http_client,
    resolve_connection,
)
from robotics_agent.adapters.sap.errors import SAPError


class FakeDestinationService:
    def __init__(self, *, proxy_type="Internet", expires_in="900", tokens=True):
        self.proxy_type = proxy_type
        self.expires_in = expires_in
        self.tokens = tokens
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "oauth/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "svc-tok", "expires_in": 3600})
        self.calls += 1
        payload = {
            "destinationConfiguration": {
                "URL": "https://s4.internal:44300",
                "Authentication": "OAuth2SAMLBearerAssertion",
                "ProxyType": self.proxy_type,
            }
        }
        if self.tokens:
            payload["authTokens"] = [
                {
                    "type": "Bearer",
                    "value": f"dest-tok-{self.calls}",
                    "expiresIn": self.expires_in,
                }
            ]
        return httpx.Response(200, json=payload)


def _provider(service: FakeDestinationService) -> DestinationTokenProvider:
    client = httpx.Client(transport=httpx.MockTransport(service.handler))
    oauth = OAuth2TokenProvider(
        token_url="https://uaa.test/oauth/token",
        client_id="id",
        client_secret="secret",
        client=client,
    )
    resolver = DestinationResolver(
        service_url="https://dest.test", token_provider=oauth, client=client
    )
    return DestinationTokenProvider(resolver, "S4-PRD", skew_seconds=10)


# --- Token yenileme ---------------------------------------------------------
def test_token_onbelleklenir_gereksiz_cozumleme_yapilmaz():
    service = FakeDestinationService(expires_in="900")
    provider = _provider(service)
    assert provider() == "dest-tok-1"
    assert provider() == "dest-tok-1"
    assert service.calls == 1


def test_suresi_dolan_token_yeniden_cozumlenir():
    """Onceki surumde token omur boyu sabitti; 401 kacinilmazdi."""
    service = FakeDestinationService(expires_in="0")
    provider = _provider(service)
    assert provider() == "dest-tok-1"
    assert provider() == "dest-tok-2", "sure dolunca destination yeniden cozulmeli"
    assert service.calls == 2


def test_expiresin_yoksa_muhafazakar_kisa_omur():
    service = FakeDestinationService(expires_in=None)
    provider = _provider(service)
    snapshot = provider.snapshot()
    import time

    assert snapshot.expires_at - time.time() <= 600 + 1


def test_destination_auth_hatasi_acik_bildirilir():
    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in request.url.path:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(
            200,
            json={
                "destinationConfiguration": {"URL": "https://s4.internal"},
                "authTokens": [{"error": "principal propagation failed"}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = OAuth2TokenProvider(
        token_url="https://uaa.test/oauth/token", client_id="i", client_secret="s",
        client=client,
    )
    resolver = DestinationResolver(
        service_url="https://dest.test", token_provider=oauth, client=client
    )
    with pytest.raises(SAPError, match="DESTINATION_AUTH_FAILED|kimlik dogrulama"):
        resolver.fetch("S4-PRD")


# --- Cloud Connector yonlendirme -------------------------------------------
def test_on_premise_proxy_yapilandirilmamissa_acik_hata(settings_factory, tmp_path):
    """Sessiz timeout yerine ne yapilmasi gerektigini soyleyen hata."""
    settings = settings_factory(
        tmp_path,
        **{
            "sap.backend": "odata", "sap.auth_mode": "destination",
            "sap.destination_name": "S4-PRD",
            "sap.destination_service_url": "https://dest.test",
            "sap.oauth_token_url": "https://uaa.test/oauth/token",
            "sap.oauth_client_id": "i", "sap.oauth_client_secret": "s",
        },
    )
    service = FakeDestinationService(proxy_type="OnPremise")
    client = httpx.Client(transport=httpx.MockTransport(service.handler))

    import robotics_agent.adapters.sap.destination as mod

    original = mod.httpx.Client
    mod.httpx.Client = lambda **kw: client  # noqa: ARG005
    try:
        with pytest.raises(SAPError, match="CONNECTIVITY_PROXY_MISSING|OnPremise"):
            resolve_connection(settings.sap)
    finally:
        mod.httpx.Client = original


def test_on_premise_trafigi_connectivity_proxyye_yonlenir(settings_factory, tmp_path):
    settings = settings_factory(
        tmp_path,
        **{
            "sap.backend": "odata", "sap.auth_mode": "destination",
            "sap.destination_name": "S4-PRD",
            "sap.destination_service_url": "https://dest.test",
            "sap.oauth_token_url": "https://uaa.test/oauth/token",
            "sap.oauth_client_id": "i", "sap.oauth_client_secret": "s",
            "sap.connectivity_proxy_url": "http://connectivity-proxy:20003",
            "sap.connectivity_token_url": "https://uaa.test/oauth/token",
            "sap.connectivity_client_id": "ci",
            "sap.connectivity_client_secret": "cs",
            "sap.cloud_connector_location_id": "FABRIKA-1",
        },
    )
    service = FakeDestinationService(proxy_type="OnPremise")
    client = httpx.Client(transport=httpx.MockTransport(service.handler))

    import robotics_agent.adapters.sap.destination as mod

    original = mod.httpx.Client
    mod.httpx.Client = lambda **kw: client  # noqa: ARG005
    try:
        connection = resolve_connection(settings.sap)
    finally:
        mod.httpx.Client = original

    assert connection.is_on_premise
    assert connection.proxy_url == "http://connectivity-proxy:20003"
    assert connection.location_id == "FABRIKA-1"
    assert connection.proxy_auth_provider is not None
    # Token header'a gomulmedi; saglayici uzerinden her istekte tazelenir.
    assert "Authorization" not in connection.headers
    assert connection.token_provider is not None

    described = connection.describe()
    assert described["via_connectivity_proxy"] is True
    assert "dest-tok" not in str(described), "token teshis ciktisina sizmamali"


def test_internet_destinationda_proxy_kullanilmaz(settings_factory, tmp_path):
    settings = settings_factory(
        tmp_path,
        **{
            "sap.backend": "odata", "sap.auth_mode": "destination",
            "sap.destination_name": "S4-CLD",
            "sap.destination_service_url": "https://dest.test",
            "sap.oauth_token_url": "https://uaa.test/oauth/token",
            "sap.oauth_client_id": "i", "sap.oauth_client_secret": "s",
            "sap.connectivity_proxy_url": "http://connectivity-proxy:20003",
        },
    )
    service = FakeDestinationService(proxy_type="Internet")
    client = httpx.Client(transport=httpx.MockTransport(service.handler))

    import robotics_agent.adapters.sap.destination as mod

    original = mod.httpx.Client
    mod.httpx.Client = lambda **kw: client  # noqa: ARG005
    try:
        connection = resolve_connection(settings.sap)
    finally:
        mod.httpx.Client = original
    assert connection.proxy_url == ""
    assert build_http_client(connection, settings.sap) is not None
