"""Kaydedilmis SAP OData metadata'sina karsi kontrat testleri.

Kaydedilmis `$metadata` anlik goruntulerine karsi calisir; gercek SAP sistemi
gerektirmez. Amac: kodun bekledigi entity set ve alan adlari servis sozlesmesiyle
uyusuyor mu? Uyusmuyorsa bunu CI'da gormek, uretimde sessiz bos veri almaktan
iyidir.

Fixture'lar kirpilmistir (yalniz kullanilan alanlar). Gercek bir sisteme baglanan
kurulumda `sap_discover_capabilities` ayni dogrulamayi canli metadata uzerinde yapar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robotics_agent.adapters.sap import (
    CAPABILITY_MANIFEST,
    STATUS_DEPRECATED,
    STATUS_RELEASED,
    parse_metadata,
    preferred_alias,
    verify_contract,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- EDMX cozumleme --------------------------------------------------------
def test_parses_v4_metadata():
    contract = parse_metadata(load("API_PURCHASEREQUISITION_2_metadata.xml"))
    assert contract.version == "4.0"
    assert contract.has_set("PurchaseRequisition")
    assert contract.has_set("PurchaseRequisitionItem")
    props = contract.properties_of_set("PurchaseRequisitionItem")
    assert "RequestedQuantity" in props
    assert "WBSElement" in props


def test_parses_v2_metadata():
    contract = parse_metadata(load("API_PRODUCT_SRV_metadata.xml"))
    assert contract.version == "1.0"
    assert contract.has_set("A_ProductDescription")
    assert "ProductDescription" in contract.properties_of_set("A_ProductDescription")


def test_empty_or_invalid_metadata_is_handled():
    assert parse_metadata("").entity_sets == {}
    assert parse_metadata("<not-xml").entity_sets == {}


# --- Manifest dogrulamasi ---------------------------------------------------
def test_purchase_requisition_contract_matches_manifest():
    """Kodun PR icin bekledigi tum kritik alanlar servis sozlesmesinde var mi?"""
    contract = parse_metadata(load("API_PURCHASEREQUISITION_2_metadata.xml"))
    check = verify_contract(CAPABILITY_MANIFEST["purchase_requisition"], contract)
    assert check.contract_ok, check.to_dict()
    assert check.missing_entity_sets == ()
    assert check.missing_properties == {}


def test_product_contract_matches_manifest():
    contract = parse_metadata(load("API_PRODUCT_SRV_metadata.xml"))
    check = verify_contract(CAPABILITY_MANIFEST["product"], contract)
    assert check.contract_ok, check.to_dict()


def test_broken_contract_is_detected():
    """Eksik alan/entity set sessizce gecmemeli."""
    contract = parse_metadata(load("API_PURCHASEREQUISITION_2_metadata_broken.xml"))
    check = verify_contract(CAPABILITY_MANIFEST["purchase_requisition"], contract)
    assert not check.contract_ok
    assert "PurchaseRequisition" in check.missing_entity_sets
    missing_item_props = check.missing_properties.get("PurchaseRequisitionItem", ())
    assert "PurchaseRequisitionPrice" in missing_item_props


# --- Tercih sirasi ---------------------------------------------------------
def test_released_v4_is_preferred_over_deprecated_v2():
    assert (
        preferred_alias("purchase_requisition_v2", "purchase_requisition")
        == "purchase_requisition"
    )
    assert preferred_alias("purchase_order_v2", "purchase_order") == "purchase_order"


def test_manifest_marks_v2_fallbacks_as_deprecated():
    assert CAPABILITY_MANIFEST["purchase_requisition_v2"].status == STATUS_DEPRECATED
    assert CAPABILITY_MANIFEST["purchase_requisition_v2"].successor == "purchase_requisition"
    assert CAPABILITY_MANIFEST["purchase_requisition"].status == STATUS_RELEASED


@pytest.mark.parametrize("alias", sorted(CAPABILITY_MANIFEST))
def test_every_manifest_entry_is_well_formed(alias):
    capability = CAPABILITY_MANIFEST[alias]
    assert capability.service_path.startswith("/sap/opu/odata")
    assert capability.odata_version in {"v2", "v4"}
    assert capability.entity_sets, alias
    assert capability.purpose, alias
    # Kritik alan beyani olan her entity set, entity_sets icinde de olmali.
    for entity_set in capability.critical_properties:
        assert entity_set in capability.entity_sets, f"{alias}: {entity_set}"


def test_v4_service_paths_use_odata4_root():
    for alias, capability in CAPABILITY_MANIFEST.items():
        if capability.odata_version == "v4":
            assert "/odata4/" in capability.service_path, alias
