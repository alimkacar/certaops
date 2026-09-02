"""SAP adapter katmani.

Tercih sirasi released OData V4, released OData V2/SOAP ve kontrollu released
custom API'dir. Dogrudan tablo okuma ve GUI RPA yalnizca belgelenmis istisnadir.
"""

from .breaker import CircuitBreaker, CircuitOpen, breaker_for, null_breaker
from .capabilities import (
    CAPABILITY_MANIFEST,
    STATUS_CUSTOM,
    STATUS_DEPRECATED,
    STATUS_RELEASED,
    CapabilityCheck,
    MetadataContract,
    NavigationInfo,
    ServiceCapability,
    WriteShapeIssue,
    WriteShapeReport,
    account_assignment_shape,
    manifest_summary,
    parse_metadata,
    preferred_alias,
    verify_contract,
    verify_write_shape,
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
from .http import HostNotAllowed, ODataHttpCore, ODataResponse, SAPCallBudget
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
    "CircuitBreaker",
    "CircuitOpen",
    "DestinationResolver",
    "HostNotAllowed",
    "MetadataContract",
    "NavigationInfo",
    "OAuth2TokenProvider",
    "ODataHttpCore",
    "ODataResponse",
    "ODataV2Client",
    "ODataV4Client",
    "Page",
    "ResolvedConnection",
    "SAPError",
    "SAPCallBudget",
    "SAPFault",
    "SAPNotSupported",
    "ServiceCapability",
    "WriteShapeIssue",
    "WriteShapeReport",
    "account_assignment_shape",
    "breaker_for",
    "build_http_client",
    "escape_key",
    "expanded_rows",
    "explain_authorization_failure",
    "manifest_summary",
    "null_breaker",
    "parse_metadata",
    "parse_odata_datetime",
    "parse_sap_error",
    "preferred_alias",
    "quote",
    "resolve_connection",
    "to_odata_datetime",
    "verify_contract",
    "verify_write_shape",
]
