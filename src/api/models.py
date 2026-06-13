# /src/api/models.py

import uuid
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
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

class ErrorDetail(BaseModel):
    detail: str
    session_id: Optional[str] = Field(
        default=None,
        description="Session ID if available"
    )
    error_code: Optional[str] = Field(
        default=None,
        description="Error code for programmatic handling"
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
    exp: int = Field(..., description="Expiry timestamp (Unix epoch)")
    iat: Optional[int] = Field(default=None, description="Issued-at timestamp (Unix epoch)")

# --- File Upload Models ---

class FileUploadRequest(BaseModel):
    """Request model for file upload operations."""
    project_id: str = Field(
        ...,
        min_length=1,
        description="The project ID for tracking and organization.",
        json_schema_extra={'example': "my-project-123"}
    )
    filename: Optional[str] = Field(
        default=None,
        description="Optional custom filename for the uploaded file"
    )

class FileUploadResponse(BaseResponse):
    """Response model for file upload operations."""
    status: str = Field(..., description="Status of the operation")
    message: str = Field(..., description="Human-readable message about the operation")
    file_id: Optional[str] = Field(
        default=None,
        description="ID of the uploaded file"
    )
    file_size: Optional[int] = Field(
        default=None,
        description="Size of the uploaded file in bytes"
    )
    file_path: Optional[str] = Field(
        default=None,
        description="Local path where the file was saved"
    )