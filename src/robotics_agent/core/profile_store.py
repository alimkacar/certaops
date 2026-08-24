"""Tenant profillerinin ve SAP redlerinin kalici deposu.

Profil KOD DEGIL VERI oldugu icin bir yerde saklanmasi gerekir. Ortam
degiskeni yeterli degildir: cok tenant'li bir kurulumda her tenant'in kendi
belge tipi ve zorunlu alanlari olur, ama `Settings` tek kurulum icin tektir.

Depo iki sey tutar:

  * **profil** - operatorun bildirdigi sirket gercekleri,
  * **red kaydi** - SAP'in gozlemlenmis reddi.

Ikincisi otomatik olarak birincisine donusmez. Tek seferlik bir red bir kural
degildir; "bu alan bu sirkette hep zorunlu" karari insanindir. Depo yalnizca
kanit biriktirir, karari operatore birakir.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .store import StateDatabase
from .tenant_profile import RejectionRecord, SapTenantProfile, _now

log = logging.getLogger(__name__)


class TenantProfileStore:
    """Tenant profili ve red kaydi icin SQLite destekli depo."""

    def __init__(self, db: StateDatabase) -> None:
        self.db = db

    # --- Profil -------------------------------------------------------------
    def load(self, tenant: str) -> SapTenantProfile:
        """Tenant profili. Tanimli degilse SAP standardi varsayilanlar doner.

        Hicbir zaman None donmez: cagri yerlerinin "profil var mi" diye
        dallanmasi gerekmesin. Profil yoklugu bir hata degil, gecerli bir
        durumdur (standart SAP davranisi).
        """
        row = self.db.connection.execute(
            "SELECT * FROM sap_tenant_profile WHERE tenant = ?", (tenant,)
        ).fetchone()
        if row is None:
            return SapTenantProfile.default(tenant)
        return SapTenantProfile.from_row(dict(row))

    def save(self, profile: SapTenantProfile) -> SapTenantProfile:
        """Profili yazar ve yazilan hali (updated_at dahil) dondurur."""
        if not profile.tenant:
            raise ValueError("Profil tenant'siz kaydedilemez.")
        stamped = _now()
        conn = self.db.connection
        with conn:
            conn.execute(
                """
                INSERT INTO sap_tenant_profile
                    (tenant, document_type, required_fields_json, defaults_json,
                     field_map_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant) DO UPDATE SET
                    document_type        = excluded.document_type,
                    required_fields_json = excluded.required_fields_json,
                    defaults_json        = excluded.defaults_json,
                    field_map_json       = excluded.field_map_json,
                    updated_at           = excluded.updated_at
                """,
                (
                    profile.tenant,
                    profile.document_type,
                    json.dumps(list(profile.required_fields), ensure_ascii=False),
                    json.dumps(profile.default_values, ensure_ascii=False),
                    json.dumps(profile.field_mapping, ensure_ascii=False),
                    stamped,
                ),
            )
        log.info(
            "Tenant profili guncellendi | tenant=%s | belge_tipi=%s | zorunlu_alan=%d",
            profile.tenant, profile.document_type, len(profile.required_fields),
        )
        return self.load(profile.tenant)

    # --- Red kaydi ----------------------------------------------------------
    def record_rejection(
        self, tenant: str, *, tool: str, sap_code: str = "", field: str = "", message: str = ""
    ) -> None:
        """SAP'in bir yazmayi reddettigini kaydeder.

        Ayni (tenant, tool, kod, alan) dortlusu tekrarlanirsa yeni satir
        acilmaz, sayac artar: "bu bir kez oldu" ile "bu her seferinde oluyor"
        arasindaki fark, kurala yukseltme kararinin dayanagidir.
        """
        if not tenant or not tool:
            return
        stamped = _now()
        conn = self.db.connection
        with conn:
            conn.execute(
                """
                INSERT INTO sap_rejection_log
                    (tenant, tool, sap_code, field, message, seen_count, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(tenant, tool, sap_code, field) DO UPDATE SET
                    seen_count = seen_count + 1,
                    message    = excluded.message,
                    last_seen  = excluded.last_seen
                """,
                (tenant, tool, sap_code, field, message[:500], stamped, stamped),
            )

    def rejections(self, tenant: str, *, limit: int = 50) -> list[RejectionRecord]:
        """Bu tenant'ta gozlenmis redler; en sik gorulen basta."""
        rows = self.db.connection.execute(
            """
            SELECT * FROM sap_rejection_log WHERE tenant = ?
            ORDER BY seen_count DESC, last_seen DESC LIMIT ?
            """,
            (tenant, limit),
        ).fetchall()
        return [
            RejectionRecord(
                tenant=row["tenant"], tool=row["tool"], sap_code=row["sap_code"],
                field=row["field"], message=row["message"], seen_count=row["seen_count"],
                first_seen=row["first_seen"], last_seen=row["last_seen"],
            )
            for row in rows
        ]

    def promote_to_required(self, tenant: str, field: str) -> SapTenantProfile:
        """Gozlenmis bir reddi kalici kurala cevirir.

        Bilerek ayri bir cagri: otomatik yukseltme, tek seferlik bir SAP
        hatasini kaliici bir kisitlamaya donusturur ve yanlis olursa gecerli
        talepleri engeller. Karar insanindir.
        """
        profile = self.load(tenant)
        if field in profile.required_fields:
            return profile
        updated = SapTenantProfile(
            tenant=profile.tenant or tenant,
            document_type=profile.document_type,
            required_fields=(*profile.required_fields, field),
            defaults=profile.defaults,
            field_map=profile.field_map,
        )
        log.info("Red kurala yukseltildi | tenant=%s | alan=%s", tenant, field)
        return self.save(updated)

    def describe(self, tenant: str) -> dict[str, Any]:
        """Profil + gozlenmis redler (teshis ciktisi)."""
        return {
            "profile": self.load(tenant).describe(),
            "observed_rejections": [r.to_dict() for r in self.rejections(tenant, limit=20)],
        }
