#!/usr/bin/env python3
"""CAL S/4 baglantisi, TLS, allowlist, istemci ve dry-run guvenlik kapisi."""

from __future__ import annotations

import argparse
from urllib.parse import urlparse

from cal_acceptance_lib import add_common_args, load_profile, make_report, target_summary, timed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args(argv)
    report, started = make_report("CAL-01", "Baglanti ve guvenli profil")

    try:
        profile = load_profile(args.env_file)
    except FileNotFoundError as exc:
        report.check("CAL profili yuklendi", False, exc)
        return report.finish(started, out=args.out or "artifacts/cal/01-connection.json")

    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    parsed = urlparse(settings.sap.base_url)
    report.check("CAL profili yuklendi", True, profile)
    report.check("Gercek OData backend", settings.sap.backend == "odata", settings.sap.backend)
    report.check(
        "API Hub degil CAL hedefi",
        parsed.hostname not in {"sandbox.api.sap.com", "api.sap.com"},
        parsed.hostname or "host yok",
    )
    report.check("HTTPS kullaniliyor", parsed.scheme == "https", parsed.scheme)
    report.check("TLS dogrulamasi acik", settings.sap.verify_ssl, settings.sap.verify_ssl)
    report.check(
        "Hedef host allowlist'te",
        bool(parsed.hostname and parsed.hostname in settings.security.allowed_sap_hosts),
        settings.security.allowed_sap_hosts,
    )
    report.check("SAP istemcisi acik", bool(settings.sap.client), settings.sap.client)
    report.check("Ilk kosuda yazma kapali", settings.sap.dry_run, f"dry_run={settings.sap.dry_run}")
    report.check(
        "Harici yazma kapisi kapali",
        __import__("os").getenv("SAP_INTEGRATION_ALLOW_WRITE", "0") != "1",
        "SAP_INTEGRATION_ALLOW_WRITE=0 beklenir",
    )
    problems = settings.sap.validate()
    report.check("SAP ayarlari yapisal olarak gecerli", not problems, problems or "uygun")

    backend = None
    try:
        backend, elapsed, _, error = timed(lambda: build_backend(settings))
        if error:
            report.check("OData istemcisi olusturuldu", False, error, duration_ms=elapsed)
            return report.finish(started, out=args.out or "artifacts/cal/01-connection.json")
        report.target = target_summary(settings, backend)
        health, elapsed, calls, error = timed(backend.ping, backend)
        report.check(
            "S/4 canli baglanti",
            not error and isinstance(health, dict) and health.get("status") == "ok",
            error or health,
            duration_ms=elapsed,
            sap_calls=calls,
        )
        if hasattr(backend, "connection"):
            connection = backend.connection.describe()
            report.check(
                "Kimlik bilgisi rapora sizmiyor",
                not any(
                    token in str(connection).lower()
                    for token in ("password", "secret", "api_key")
                ),
                {k: v for k, v in connection.items() if k != "warnings"},
            )
    finally:
        if backend is not None:
            backend.close()
    return report.finish(started, out=args.out or "artifacts/cal/01-connection.json")


if __name__ == "__main__":
    raise SystemExit(main())
