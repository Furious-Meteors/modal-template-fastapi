import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from src.api import handler
from src.api.auth import get_current_user
from src.api.models import (
    GenericRequest,
    HealthCheckResponse,
    HealthStatus,
    ItemResponse,
)
from src.utils.config import APP_ENV, APP_NAME, APP_VERSION

router = APIRouter()


@router.get("/health", response_model=HealthCheckResponse, tags=["Health"])
async def health_check():
    return HealthCheckResponse(
        session_id=str(uuid.uuid4()),
        status=HealthStatus.HEALTHY,
        service_name=f'{APP_NAME}-{APP_ENV}',
        version=APP_VERSION,
        services_summary={"total": 1, "healthy": 1, "unhealthy": 0},
    )


@router.get("/items", response_model=List[dict], tags=["Items"])
async def list_items(current_user: dict = Depends(get_current_user)):
    return handler.list_items()


@router.get("/items/{item_id}", response_model=ItemResponse, tags=["Items"])
async def get_item(item_id: str, current_user: dict = Depends(get_current_user)):
    item = handler.get_item(item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )
    return ItemResponse(
        session_id=str(uuid.uuid4()),
        status="success",
        message="Item retrieved successfully",
        data=item,
    )


@router.post(
    "/items",
    response_model=ItemResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Items"],
)
async def create_item(request: GenericRequest, current_user: dict = Depends(get_current_user)):
    item = handler.create_item(request)
    return ItemResponse(
        session_id=str(uuid.uuid4()),
        status="created",
        message="Item created successfully",
        data=item,
    )


@router.put("/items/{item_id}", response_model=ItemResponse, tags=["Items"])
async def update_item(item_id: str, request: GenericRequest, current_user: dict = Depends(get_current_user)):
    item = handler.update_item(item_id, request)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )
    return ItemResponse(
        session_id=str(uuid.uuid4()),
        status="updated",
        message="Item updated successfully",
        data=item,
    )


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Items"],
)
async def delete_item(item_id: str, current_user: dict = Depends(get_current_user)):
    if not handler.delete_item(item_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )
