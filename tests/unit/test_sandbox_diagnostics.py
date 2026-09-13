"""Sandbox diagnosis must stay scoped and must report failed probes as failures."""
from io import BytesIO
from urllib.error import HTTPError

import pytest

from scripts import teshis_401


def test_generic_basic_does_not_claim_gateway_dropped_credentials():
    assert teshis_401.tanı(401, b"", "text/html", 'Basic realm="proxy"') == "KIMLIK REDDI"
    assert teshis_401.tanı(
        401, b"", "text/html", 'Basic realm="SAP NetWeaver Application Server"'
    ) == "SAP OTURUM REDDI"


def test_diagnostics_do_not_follow_redirects():
    assert teshis_401.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test") is None


@pytest.mark.parametrize("base", ["https://other.test", "http://sandbox.api.sap.com/s4hanacloud"])
def test_diagnostics_reject_other_targets_before_sending_key(monkeypatch, base):
    monkeypatch.setattr(teshis_401, "env_yukle", lambda: {"SAP_BASE_URL": base, "SAP_API_KEY": "secret"})
    monkeypatch.setattr(teshis_401.urllib.request, "build_opener", lambda *_: pytest.fail("network"))
    assert teshis_401.main() == 2


def test_failed_probes_exit_nonzero_and_do_not_print_key(monkeypatch, capsys):
    key = "private-prefix-secret-suffix"
    monkeypatch.setattr(teshis_401, "env_yukle", lambda: {"SAP_API_KEY": key})
    urls = []

    class FailedSandbox:
        def open(self, request, **kwargs):
            urls.append(request.full_url)
            raise HTTPError(request.full_url, 401, "Unauthorized", {
                "content-type": "text/html",
                "www-authenticate": 'Basic realm="SAP NetWeaver Application Server"',
            }, BytesIO(b"<html>Logon failed</html>"))

    monkeypatch.setattr(teshis_401.urllib.request, "build_opener", lambda *_: FailedSandbox())
    assert teshis_401.main() == 1
    out = capsys.readouterr().out
    assert key not in out
    assert "ilk4" not in out and "son4" not in out
    assert all(url.startswith("https://sandbox.api.sap.com/s4hanacloud/") for url in urls)
    assert "SAP oturum acmayi reddetti" in out
