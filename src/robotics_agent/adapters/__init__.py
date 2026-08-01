"""Dis sistem adaptorleri: SAP OData, BTP Destination ve Build Process Automation."""

from .bpa import (
    ApprovalGateway,
    ApprovalRequest,
    BPAApprovalGateway,
    LocalApprovalGateway,
    build_approval_gateway,
)

__all__ = [
    "ApprovalGateway",
    "ApprovalRequest",
    "BPAApprovalGateway",
    "LocalApprovalGateway",
    "build_approval_gateway",
]
