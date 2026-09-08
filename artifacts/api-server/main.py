from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("papa-bot")

CODE_RE = re.compile(r"^[A-Za-z0-9]{6}$")
DEFAULT_SYSTEM_PROMPT = """Ты — доброжелательный собеседник для пожилого человека.
Говори по-русски, тепло и простыми словами. Отвечай коротко, без сложных терминов,
и задавай один естественный открытый вопрос, когда это уместно.
Не осуждай и не читай нотации. Если человек сообщает об угрозе жизни, тяжёлой
абстиненции, суицидальных мыслях или резком ухудшении здоровья, спокойно посоветуй
обратиться к близкому человеку или вызвать скорую помощь. Не изображай врача."""


class Settings:
    def __init__(self) -> None:
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_base_url = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        ).rstrip("/")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.openai_timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
        self.system_prompt = os.getenv(
            "PAPA_BOT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT
        ).strip()
        self.sessions_dir = Path(
            os.getenv("PAPA_BOT_SESSIONS_DIR", "data/sessions")
        )
        self.cors_origins = [
            origin.strip()
            for origin in os.getenv("CORS_ORIGINS", "*").split(",")
            if origin.strip()
        ]


settings = Settings()


class DialogMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class DialogResponse(BaseModel):
    code: str
    messages: list[DialogMessage]


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class SendMessageResponse(DialogResponse):
    reply: str


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, code: str) -> asyncio.Lock:
        if code not in self._locks:
            self._locks[code] = asyncio.Lock()
        return self._locks[code]

    def _path_for(self, code: str) -> Path:
        return self.directory / f"{code}.json"

    def load(self, code: str) -> list[DialogMessage]:
        path = self._path_for(code)
        if not path.exists():
            return []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [DialogMessage.model_validate(item) for item in payload["messages"]]
        except (OSError, KeyError, TypeError, ValueError) as error:
            logger.exception("Could not read session %s: %s", code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось прочитать историю этого разговора.",
            ) from error

    def save(self, code: str, messages: list[DialogMessage]) -> None:
        path = self._path_for(code)
        temporary_path = path.with_suffix(".json.tmp")
        payload = {
            "code": code,
            "updated_at": datetime.now(UTC).isoformat(),
            "messages": [message.model_dump(mode="json") for message in messages],
        }

        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(path)
        except OSError as error:
            logger.exception("Could not save session %s: %s", code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось сохранить историю разговора.",
            ) from error


class MissingOpenAIKeyError(RuntimeError):
    pass


class OpenAIClient:
    async def complete(self, messages: list[DialogMessage]) -> str:
        if not settings.openai_api_key:
            raise MissingOpenAIKeyError

        request_messages = [
            {"role": "system", "content": settings.system_prompt},
            *[
                {"role": message.role, "content": message.content}
                for message in messages
            ],
        ]

        try:
            async with httpx.AsyncClient(timeout=settings.openai_timeout) as client:
                response = await client.post(
                    f"{settings.openai_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.openai_model,
                        "messages": request_messages,
                        "temperature": 0.7,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as error:
            logger.exception("OpenAI request failed: %s", error)
            raise HTTPException(
                status_code=502,
                detail="Собеседник сейчас не отвечает. Попробуйте ещё раз.",
            ) from error

        try:
            reply = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            logger.error("Unexpected OpenAI response shape")
            raise HTTPException(
                status_code=502,
                detail="Собеседник вернул неполный ответ. Попробуйте ещё раз.",
            ) from error

        if not isinstance(reply, str) or not reply.strip():
            raise HTTPException(
                status_code=502,
                detail="Собеседник вернул пустой ответ. Попробуйте ещё раз.",
            )
        return reply.strip()


store = SessionStore(settings.sessions_dir)
openai_client = OpenAIClient()


def normalize_code(raw_code: str) -> str:
    code = raw_code.lower()
    if not CODE_RE.fullmatch(code):
        raise HTTPException(
            status_code=404,
            detail="Ссылка на разговор должна содержать 6 букв или цифр.",
        )
    return code


def response_for(code: str, messages: list[DialogMessage]) -> DialogResponse:
    return DialogResponse(code=code, messages=messages)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Papa-bot API started with sessions=%s model=%s openai_configured=%s",
        settings.sessions_dir,
        settings.openai_model,
        bool(settings.openai_api_key),
    )
    yield


app = FastAPI(title="Папа-бот API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/healthz")
async def healthz() -> dict[str, str | bool]:
    return {"status": "ok", "openai_configured": bool(settings.openai_api_key)}


@app.get(
    "/api/dialog/{code}",
    response_model=DialogResponse,
)
async def get_dialog(
    code: str = PathParam(...),
) -> DialogResponse:
    normalized_code = normalize_code(code)
    async with store.lock_for(normalized_code):
        messages = store.load(normalized_code)
    return response_for(normalized_code, messages)


@app.post(
    "/api/dialog/{code}/messages",
    response_model=SendMessageResponse,
)
async def send_message(
    request: SendMessageRequest,
    code: str = PathParam(...),
) -> SendMessageResponse:
    normalized_code = normalize_code(code)
    async with store.lock_for(normalized_code):
        existing_messages = store.load(normalized_code)
        user_message = DialogMessage(
            id=f"user-{datetime.now(UTC).timestamp()}",
            role="user",
            content=request.message.strip(),
            created_at=datetime.now(UTC),
        )
        context = [*existing_messages, user_message]

        try:
            reply = await openai_client.complete(context)
        except MissingOpenAIKeyError as error:
            raise HTTPException(
                status_code=503,
                detail="OpenAI API пока не настроен на сервере.",
            ) from error

        assistant_message = DialogMessage(
            id=f"assistant-{datetime.now(UTC).timestamp()}",
            role="assistant",
            content=reply,
            created_at=datetime.now(UTC),
        )
        updated_messages = [*context, assistant_message]
        store.save(normalized_code, updated_messages)

    return SendMessageResponse(
        code=normalized_code,
        reply=reply,
        messages=updated_messages,
    )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
    )