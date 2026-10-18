# /src/api/models.py

import uuid
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, field_validator
from enum import Enum

class HealthStatus(str, Enum):
    """Enum for health check status."""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"

# --- Base Models ---

class BaseResponse(BaseModel):
    """Base response model with common fields."""
    session_id: str = Field(
        ...,
        description="Unique session ID for tracking this request",
        json_schema_extra={'example': "550e8400-e29b-41d4-a716-446655440000"}
    )

class FieldError(BaseModel):
    """One offending field in a request-validation failure."""
    field: str = Field(..., description="Dotted location, e.g. 'body.name' or 'query.limit'")
    message: str = Field(..., description="What is wrong with the value")
    type: str = Field(..., description="Machine-readable validation error type")


class ErrorDetail(BaseModel):
    """The single error body returned for every error response."""
    detail: str
    session_id: Optional[str] = Field(
        default=None,
        description="Session ID if available"
    )
    error_code: Optional[str] = Field(
        default=None,
        description="Error code for programmatic handling"
    )
    errors: Optional[list[FieldError]] = Field(
        default=None,
        description="Per-field details; present only for validation errors",
    )

# --- Request/Response Models ---

class ItemRequest(BaseModel):
    """
    Typed request for item create/update operations.
    Replace these fields with your domain-specific schema.
    """
    name: str = Field(..., min_length=1, description="Item name")
    description: Optional[str] = Field(None, description="Optional description")
    project_id: Optional[str] = Field(
        None,
        min_length=1,
        description="Project identifier for grouping and tracking",
        json_schema_extra={"example": "my-project-123"},
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class ItemResponse(BaseResponse):
    """Response model for single item operations."""
    status: str = Field(..., description="Status of the operation")
    message: str = Field(..., description="Human-readable message about the operation")
    data: Optional[Dict[str, Any]] = Field(default=None, description="Item data")


class PaginatedItemsResponse(BaseModel):
    """Paginated list response for item collections."""
    items: list[Dict[str, Any]] = Field(..., description="Page of items")
    total: int = Field(..., description="Total number of items across all pages")
    limit: int = Field(..., description="Maximum items returned in this page")
    offset: int = Field(..., description="Number of items skipped")
    has_more: bool = Field(..., description="True when more items exist beyond this page")

class HealthCheckResponse(BaseResponse):
    """Response model for health check."""
    status: HealthStatus
    service_name: str
    version: str
    services_summary: Dict[str, Any] = Field(
        ...,
        description="Summary of services health status",
        json_schema_extra={'example': {"total": 6, "healthy": 6, "unhealthy": 0}}
    )

# --- Auth Models ---

class TokenPayload(BaseModel):
    """Decoded JWT token payload."""
    sub: str = Field(..., description="Subject — typically a user ID or email")
    exp: int = Field(..., description="Expiry timestamp (Unix epoch; fractional seconds are truncated)")
    iat: Optional[int] = Field(default=None, description="Issued-at timestamp (Unix epoch)")
    scopes: list[str] = Field(default_factory=list, description="Granted authorisation scopes")

    @field_validator("exp", "iat", mode="before")
    @classmethod
    def _truncate_float_timestamps(cls, value):
        # RFC 7519 NumericDate may be fractional; some issuers emit floats.
        if isinstance(value, float) and value == value:
            return int(value)
        return value
