from fastapi import APIRouter, Depends, Query, status

from src.api.auth import require_scope
from src.api.errors import AppError, get_session_id
from src.api.models import (
    ErrorDetail,
    HealthCheckResponse,
    HealthStatus,
    ItemRequest,
    ItemResponse,
    PaginatedItemsResponse,
    TokenPayload,
)
from src.core.services.item_service import ItemService
from src.deps import get_item_service
from src.utils.config import APP_ENV, APP_NAME, APP_VERSION

router = APIRouter()

MAX_PAGE_SIZE = 500


def _item_not_found(item_id: str) -> AppError:
    return AppError(404, "ITEM_NOT_FOUND", f"Item '{item_id}' not found")


@router.get("/health", response_model=HealthCheckResponse, tags=["Health"])
async def health_check(session_id: str = Depends(get_session_id)):
    return HealthCheckResponse(
        session_id=session_id,
        status=HealthStatus.HEALTHY,
        service_name=f"{APP_NAME}-{APP_ENV}",
        version=APP_VERSION,
        services_summary={"total": 1, "healthy": 1, "unhealthy": 0},
    )


@router.get("/items", response_model=PaginatedItemsResponse, tags=["Items"])
async def list_items(
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE, description="Maximum items to return"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    service: ItemService = Depends(get_item_service),
    session_id: str = Depends(get_session_id),
    current_user: TokenPayload = Depends(require_scope("items:read")),
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
    session_id: str = Depends(get_session_id),
    current_user: TokenPayload = Depends(require_scope("items:read")),
):
    item = await service.get(item_id)
    if item is None:
        raise _item_not_found(item_id)
    return ItemResponse(
        session_id=session_id,
        status="success",
        message="Item retrieved successfully",
        data=item.to_dict(),
    )


@router.post(
    "/items",
    response_model=ItemResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorDetail, "description": "Item name already exists"}},
    tags=["Items"],
)
async def create_item(
    request: ItemRequest,
    service: ItemService = Depends(get_item_service),
    session_id: str = Depends(get_session_id),
    current_user: TokenPayload = Depends(require_scope("items:write")),
):
    item = await service.create(
        name=request.name,
        description=request.description,
        project_id=request.project_id,
    )
    return ItemResponse(
        session_id=session_id,
        status="created",
        message="Item created successfully",
        data=item.to_dict(),
    )


@router.put(
    "/items/{item_id}",
    response_model=ItemResponse,
    tags=["Items"],
    responses={409: {"model": ErrorDetail, "description": "Item name already exists"}},
)
async def update_item(
    item_id: str,
    request: ItemRequest,
    service: ItemService = Depends(get_item_service),
    session_id: str = Depends(get_session_id),
    current_user: TokenPayload = Depends(require_scope("items:write")),
):
    item = await service.update(
        item_id=item_id,
        name=request.name,
        description=request.description,
        project_id=request.project_id,
    )
    if item is None:
        raise _item_not_found(item_id)
    return ItemResponse(
        session_id=session_id,
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
    current_user: TokenPayload = Depends(require_scope("items:write")),
):
    if not await service.delete(item_id):
        raise _item_not_found(item_id)
