import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth import get_current_user, require_scope
from src.api.models import (
    HealthCheckResponse,
    HealthStatus,
    ItemRequest,
    ItemResponse,
    PaginatedItemsResponse,
)
from src.core.services.item_service import ItemService
from src.deps import get_item_service
from src.utils.config import APP_ENV, APP_NAME, APP_VERSION

router = APIRouter()


@router.get("/health", response_model=HealthCheckResponse, tags=["Health"])
async def health_check():
    return HealthCheckResponse(
        session_id=str(uuid.uuid4()),
        status=HealthStatus.HEALTHY,
        service_name=f"{APP_NAME}-{APP_ENV}",
        version=APP_VERSION,
        services_summary={"total": 1, "healthy": 1, "unhealthy": 0},
    )


@router.get("/items", response_model=PaginatedItemsResponse, tags=["Items"])
async def list_items(
    limit: int = 100,
    offset: int = 0,
    service: ItemService = Depends(get_item_service),
    current_user: dict = Depends(get_current_user),
):
    items, total = await service.list(limit=limit, offset=offset)
    return PaginatedItemsResponse(
        items=[item.to_dict() for item in items],
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.get("/items/{item_id}", response_model=ItemResponse, tags=["Items"])
async def get_item(
    item_id: str,
    service: ItemService = Depends(get_item_service),
    current_user: dict = Depends(get_current_user),
):
    item = await service.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )
    return ItemResponse(
        session_id=str(uuid.uuid4()),
        status="success",
        message="Item retrieved successfully",
        data=item.to_dict(),
    )


@router.post(
    "/items",
    response_model=ItemResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Items"],
)
async def create_item(
    request: ItemRequest,
    service: ItemService = Depends(get_item_service),
    current_user: dict = Depends(require_scope("items:write")),
):
    item = await service.create(
        name=request.name,
        description=request.description,
        project_id=request.project_id,
    )
    return ItemResponse(
        session_id=str(uuid.uuid4()),
        status="created",
        message="Item created successfully",
        data=item.to_dict(),
    )


@router.put("/items/{item_id}", response_model=ItemResponse, tags=["Items"])
async def update_item(
    item_id: str,
    request: ItemRequest,
    service: ItemService = Depends(get_item_service),
    current_user: dict = Depends(require_scope("items:write")),
):
    item = await service.update(
        item_id=item_id,
        name=request.name,
        description=request.description,
        project_id=request.project_id,
    )
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )
    return ItemResponse(
        session_id=str(uuid.uuid4()),
        status="updated",
        message="Item updated successfully",
        data=item.to_dict(),
    )


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Items"],
)
async def delete_item(
    item_id: str,
    service: ItemService = Depends(get_item_service),
    current_user: dict = Depends(require_scope("items:write")),
):
    if not await service.delete(item_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )
