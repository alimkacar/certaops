"""SAP adapter katmani.

Tercih sirasi released OData V4, released OData V2/SOAP ve kontrollu released
custom API'dir. Dogrudan tablo okuma ve GUI RPA yalnizca belgelenmis istisnadir.
"""

from .capabilities import (
    CAPABILITY_MANIFEST,
    STATUS_CUSTOM,
    STATUS_DEPRECATED,
    STATUS_RELEASED,
    CapabilityCheck,
    MetadataContract,
    ServiceCapability,
    manifest_summary,
    parse_metadata,
    preferred_alias,
    verify_contract,
)
from .destination import (
    DestinationResolver,
    OAuth2TokenProvider,
    ResolvedConnection,
    build_http_client,
    resolve_connection,
)
from .errors import (
    AuthorizationExplanation,
    SAPError,
    SAPFault,
    SAPNotSupported,
    explain_authorization_failure,
    parse_sap_error,
)
from .http import HostNotAllowed, ODataHttpCore, ODataResponse
from .odata_v2 import ODataV2Client, expanded_rows, parse_odata_datetime, to_odata_datetime
from .odata_v4 import BatchRequest, BatchResponse, ODataV4Client, Page, escape_key, quote

__all__ = [
    "CAPABILITY_MANIFEST",
    "STATUS_CUSTOM",
    "STATUS_DEPRECATED",
    "STATUS_RELEASED",
    "AuthorizationExplanation",
    "BatchRequest",
    "BatchResponse",
    "CapabilityCheck",
    "DestinationResolver",
    "HostNotAllowed",
    "MetadataContract",
    "OAuth2TokenProvider",
    "ODataHttpCore",
    "ODataResponse",
    "ODataV2Client",
    "ODataV4Client",
    "Page",
    "ResolvedConnection",
    "SAPError",
    "SAPFault",
    "SAPNotSupported",
    "ServiceCapability",
    "build_http_client",
    "escape_key",
    "expanded_rows",
    "explain_authorization_failure",
    "manifest_summary",
    "parse_metadata",
    "parse_odata_datetime",
    "parse_sap_error",
    "preferred_alias",
    "quote",
    "resolve_connection",
    "to_odata_datetime",
    "verify_contract",
]
