"""401 sonrasi otomatik yeniden baglanma testi."""
import httpx

from robotics_agent.adapters.sap.http import ODataHttpCore


def test_401_sonrasi_yeniden_baglanip_tekrar_dener():
    state = {"calls": 0}
    def handler(request):
        state["calls"] += 1
        if request.headers.get("Authorization") != "Bearer taze":
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"value": []})

    stale = httpx.Client(base_url="https://s4.test", transport=httpx.MockTransport(handler),
                         headers={"Authorization": "Bearer eski"})
    fresh = httpx.Client(base_url="https://s4.test", transport=httpx.MockTransport(handler),
                         headers={"Authorization": "Bearer taze"})
    rebuilt = {"n": 0}
    def reconnect():
        rebuilt["n"] += 1
        return fresh

    core = ODataHttpCore(client=stale, reconnect=reconnect, allowed_hosts=("s4.test",), sleep=lambda _: None)
    resp = core.request("GET", "/sap/x/Y")
    assert resp.status_code == 200
    assert rebuilt["n"] == 1, "401'de baglanti yenilenmedi"

def test_reconnect_saglayicisi_yoksa_401_hata_doner():
    import pytest

    from robotics_agent.adapters.sap.errors import SAPError
    def handler(_):
        return httpx.Response(401, json={"error": {"message": "no"}})
    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.test", transport=httpx.MockTransport(handler)),
        sleep=lambda _: None)
    with pytest.raises(SAPError):
        core.request("GET", "/sap/x/Y")

def test_yeniden_baglanma_sonsuz_donguye_girmez():
    def handler(_):
        return httpx.Response(401, json={"error": {"message": "no"}})
    tries = {"n": 0}
    def reconnect():
        tries["n"] += 1
        return httpx.Client(base_url="https://s4.test", transport=httpx.MockTransport(handler))
    import pytest

    from robotics_agent.adapters.sap.errors import SAPError
    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.test", transport=httpx.MockTransport(handler)),
        reconnect=reconnect, sleep=lambda _: None)
    with pytest.raises(SAPError):
        core.request("GET", "/sap/x/Y")
    assert tries["n"] == 1, "yeniden baglanma tur basina bir kez denenmeli"
