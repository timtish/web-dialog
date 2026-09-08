# Папа-бот

Одностраничный тёплый чат для разговоров по персональной ссылке, где шестизначный код определяет отдельную историю.

## Run & Operate

- `python artifacts/api-server/main.py` — run the FastAPI API server (port 8080)
- `pnpm --filter @workspace/dialog run dev` — run the frontend
- `pnpm run typecheck` — full typecheck across all packages
- `python -m py_compile artifacts/api-server/main.py` — check the FastAPI syntax
- Configuration template: `artifacts/api-server/.env.example`
- Sessions are stored in `data/sessions/` and are intentionally git-ignored.

## Stack

- pnpm workspace for the React/Vite frontend
- API: Python 3.13, FastAPI, Uvicorn, HTTPX
- LLM: OpenAI-compatible `/chat/completions` endpoint
- Storage: one JSON file per six-character dialog code

## Where things live

- `artifacts/dialog/` — user-facing chat at `/dialog/<code>`
- `artifacts/api-server/main.py` — FastAPI routes, session storage, and OpenAI client
- `artifacts/api-server/.env.example` — non-secret configuration reference
- `data/sessions/<code>/` — local history, bookmarks, and generated prompt files for one dialog code

## Architecture decisions

- The public identity is a six-character alphanumeric code, normalized to lowercase; there is no account or admin UI.
- The frontend route is `/dialog/<code>` and the API is `/api/dialog/<code>`.
- The OpenAI client uses configurable `OPENAI_BASE_URL`, so an OpenAI-compatible provider can be substituted without code changes.
- Conversation histories are saved atomically as JSON files instead of using the unused database scaffold.
- Character bookmarks use Yandex Search API context plus the configured LLM; both integrations remain empty until deployment configuration is provided.

## Product

- A visitor opens a personal `/dialog/<code>` link.
- The chat loads only that code's history and sends new messages to FastAPI.
- The backend forwards the conversation to the configured OpenAI-compatible API and persists successful exchanges.
- Character bookmarks can be created, moderated, switched, and stored inside the same dialog code without clearing its history.

## User preferences

- Do not add an admin dashboard or duplicate file-system monitoring in the app.

## Gotchas

- `OPENAI_API_KEY` is required for sending messages; without it, the API intentionally returns a clear 503 instead of silently using mock replies.
- Creating a character requires both OpenAI-compatible LLM settings and `YANDEX_SEARCH_API_KEY` plus `YANDEX_SEARCH_FOLDER_ID`.
- Codes must be exactly six ASCII letters or digits. Uppercase links are treated as the same lowercase session.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
