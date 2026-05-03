# API Reference

`modal-template-fastapi` exposes a RESTful JSON API versioned under `/api/v1`.

---

## Base URL

| Environment | Base URL |
|---|---|
| Local (uvicorn) | `http://localhost:8000/api/v1` |
| feat | `https://feat-app.modal.run/api/v1` |
| dev | `https://dev-app.modal.run/api/v1` |
| prod | `https://prod-app.modal.run/api/v1` |

---

## Authentication

All endpoints except `GET /health` require a **Bearer JWT** in the `Authorization` header:

```
Authorization: Bearer <your-jwt-token>
```

See the [Authentication guide](auth.md) for token format, error codes, and how to generate tokens for local testing.

---

## Common Response Fields

Every response — success or error — includes a `session_id` UUID for distributed tracing:

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "success",
  "message": "Item retrieved successfully",
  "data": { ... }
}
```

---

## Endpoints at a Glance

| Method | Path | Auth | Description |
|---|---|---|---|
| <span class="method-get">GET</span> | `/health` | None | Service health check |
| <span class="method-get">GET</span> | `/items` | JWT | List all items |
| <span class="method-post">POST</span> | `/items` | JWT | Create a new item |
| <span class="method-get">GET</span> | `/items/{item_id}` | JWT | Retrieve a single item |
| <span class="method-put">PUT</span> | `/items/{item_id}` | JWT | Replace an item |
| <span class="method-delete">DELETE</span> | `/items/{item_id}` | JWT | Delete an item |

[:octicons-arrow-right-24: Full endpoint reference with curl examples →](endpoints.md)
