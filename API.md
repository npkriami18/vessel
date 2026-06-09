# API Endpoints

## GET /health
Returns service health.

Example response:

```json
{
	"status": "ok"
}
```

## GET /users
Returns a list of users matching the `users` schema in `schema.sql`.

Example response:

```json
[
	{
		"id": 1,
		"email": "alice@example.com",
		"name": "Alice",
		"created_at": "2026-01-01T00:00:00Z"
	}
]
```

## Run locally

```sh
python api.py
```

Then open:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/users`
```
