"""SAP baglanti kimlik dogrulama katmani.

`adapters/sap/destination.py` uretimdeki kimlik dogrulamanin tamamini tasir -
OAuth2 client credentials, BTP Destination cozumlemesi, principal propagation -
ve kapsami **%31** idi. Token suresi dolunca ne oldugu, yenilemenin gercekten
calisip calismadigi, secret'larin sizip sizmadigi hic dogrulanmamisti.

Sahte olan tek sey ag.
"""

from __future__ import annotations

import httpx
import pytest

from robotics_agent.adapters.sap import SAPError
from robotics_agent.adapters.sap.destination import (
    DestinationResolver,
    OAuth2TokenProvider,
    resolve_connection,
)


class FakeIdP:
    """Token endpoint'i taklit eder; cagri sayar."""

    def __init__(self, expires_in: int = 3600, status: int = 200, payload=None) -> None:
        self.calls: list[httpx.Request] = []
        self.expires_in = expires_in
        self.status = status
        self.payload = payload
        self.token_seq = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "invalid_client"})
        if self.payload is not None:
            return httpx.Response(200, json=self.payload)
        self.token_seq += 1
        return httpx.Response(
            200,
            json={"access_token": f"tok-{self.token_seq}", "expires_in": self.expires_in},
        )


def _provider(idp: FakeIdP, **kw) -> OAuth2TokenProvider:
    return OAuth2TokenProvider(
        token_url="https://idp.test/oauth/token",
        client_id="cid", client_secret="csec",
        client=httpx.Client(transport=httpx.MockTransport(idp.handler)),
        **kw,
    )


# ---------------------------------------------------------------------------
# OAuth2 token yasam dongusu
# ---------------------------------------------------------------------------
def test_token_onbelleklenir_her_istekte_yeniden_alinmaz():
    """Her SAP cagrisinda token almak IdP'yi doverdi ve gecikme eklerdi."""
    idp = FakeIdP()
    provider = _provider(idp)
    assert provider() == "tok-1"
    assert provider() == "tok-1"
    assert provider() == "tok-1"
    assert len(idp.calls) == 1, "token onbelleklenmedi"


def test_suresi_dolan_token_yenilenir():
    idp = FakeIdP(expires_in=0)  # aninda suresi dolmus sayilir
    provider = _provider(idp, skew_seconds=10)
    assert provider() == "tok-1"
    assert provider() == "tok-2", "suresi dolan token yenilenmedi"
    assert len(idp.calls) == 2


def test_skew_penceresi_erken_yeniler():
    """Token tam suresinde degil, biraz ONCE yenilenmeli.

    Aksi halde uzun bir SAP cagrisi sirasinda token olur ve istek 401 doner.
    """
    idp = FakeIdP(expires_in=30)
    provider = _provider(idp, skew_seconds=60)  # skew > omur
    provider()
    provider()
    assert len(idp.calls) == 2, "skew penceresi uygulanmadi"


def test_idp_hatasi_acik_kodla_bildirilir():
    provider = _provider(FakeIdP(status=401))
    with pytest.raises(SAPError) as exc:
        provider()
    assert exc.value.code == "OAUTH_TOKEN_FAILED"


def test_access_token_yoksa_sessizce_bos_donmez():
    """Bos token ile devam etmek 401'i SAP tarafina otelerdi."""
    provider = _provider(FakeIdP(payload={"expires_in": 3600}))
    with pytest.raises(SAPError) as exc:
        provider()
    assert exc.value.code == "OAUTH_TOKEN_MISSING"


def test_token_saglayici_hata_mesajinda_secret_sizdirmaz():
    """Hata metni log/audit'e gider; client_secret oraya gitmemeli."""
    provider = _provider(FakeIdP(status=500))
    with pytest.raises(SAPError) as exc:
        provider()
    metin = str(exc.value) + str(getattr(exc.value, "detail", ""))
    assert "csec" not in metin
    assert "cid" not in metin


# ---------------------------------------------------------------------------
# BTP Destination cozumlemesi
# ---------------------------------------------------------------------------
def _destination_response(config: dict, auth_tokens=None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "destinationConfiguration": config,
            "authTokens": auth_tokens or [],
        },
    )


def test_destination_url_ve_proxy_tipini_cozer():
    idp = FakeIdP()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _destination_response(
            {"URL": "https://s4.internal:44300/", "ProxyType": "OnPremise",
             "Authentication": "OAuth2SAMLBearerAssertion"},
            [{"type": "Bearer", "value": "dest-token"}],
        )

    resolver = DestinationResolver(
        service_url="https://dest.test/",
        token_provider=_provider(idp),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    connection = resolver.resolve("S4-PRD")

    assert connection.base_url == "https://s4.internal:44300"
    assert connection.proxy_type == "OnPremise"
    assert captured[0].headers["Authorization"].startswith("Bearer tok-")


def test_destination_bulunamazsa_acik_hata():
    resolver = DestinationResolver(
        service_url="https://dest.test",
        token_provider=_provider(FakeIdP()),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(404, json={}))),
    )
    with pytest.raises(SAPError) as exc:
        resolver.resolve("YOK")
    assert exc.value.code == "DESTINATION_RESOLVE_FAILED"


def test_destination_url_icermiyorsa_reddedilir():
    """URL'siz destination ile devam etmek istegi bilinmeyen hedefe gonderirdi."""
    resolver = DestinationResolver(
        service_url="https://dest.test",
        token_provider=_provider(FakeIdP()),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: _destination_response({"ProxyType": "Internet"}))),
    )
    with pytest.raises(SAPError) as exc:
        resolver.resolve("S4-PRD")
    assert exc.value.code == "DESTINATION_NO_URL"


def test_basic_authentication_uyarisi_uretilir():
    """BTP Destination Basic kullaniyorsa bu gorunur olmali."""
    resolver = DestinationResolver(
        service_url="https://dest.test",
        token_provider=_provider(FakeIdP()),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: _destination_response({
                "URL": "https://s4.internal", "Authentication": "BasicAuthentication",
                "User": "svc", "Password": "pw"}))),
    )
    connection = resolver.resolve("S4-PRD")
    assert any("Basic" in w for w in connection.warnings)


# ---------------------------------------------------------------------------
# describe() - saglik/yetenek tool'larina giden ozet
# ---------------------------------------------------------------------------
def test_describe_secret_sizdirmaz(settings_factory, tmp_path):
    """`sap_connection_health` ciktisi API yanitina girer: secret icermemeli."""
    settings = settings_factory(
        tmp_path,
        **{"sap.backend": "odata", "sap.base_url": "https://s4.test",
           "sap.auth_mode": "basic", "sap.username": "svc-user",
           "sap.password": "cok-gizli-parola"},
    )
    connection = resolve_connection(settings.sap)
    ozet = connection.describe()
    dump = repr(ozet)

    assert "cok-gizli-parola" not in dump
    assert "svc-user" not in dump
    assert ozet["auth"] == "basic"
    assert any("basic" in w.lower() for w in ozet["warnings"]), (
        "uretimde onerilmeyen kimlik modeli uyari uretmeli"
    )
