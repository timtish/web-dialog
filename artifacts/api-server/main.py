from __future__ import annotations

import asyncio
import html
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
MAX_BOOKMARKS = 10
MAX_PROMPT_WORDS = 500
BOOKMARK_CREATION_COOLDOWN_SECONDS = 60
DEFAULT_SYSTEM_PROMPT = """Ты — доброжелательный собеседник для пожилого человека.
Говори по-русски, тепло и простыми словами. Отвечай коротко, без сложных терминов,
и задавай один естественный открытый вопрос, когда это уместно.
Не осуждай и не читай нотации. Если человек сообщает об угрозе жизни, тяжёлой
абстиненции, суицидальных мыслях или резком ухудшении здоровья, спокойно посоветуй
обратиться к близкому человеку или вызвать скорую помощь. Не изображай врача."""
DEFAULT_DAILY_THOUGHT = "«Хороший разговор — это тоже прогулка»"
UNIVERSAL_CHARACTER_SAFETY = """## Общая безопасность
- Не поощряй алкоголь, наркотики, насилие или самоповреждение.
- Если разговор касается алкоголя или наркотиков, мягко отговаривай и предлагай безопасную альтернативу.
- Не романтизируй зависимость, насилие, суицид или раннюю смерть.
- Если человек сообщает об угрозе жизни или резком ухудшении здоровья, спокойно посоветуй обратиться к близкому человеку, врачу или вызвать скорую помощь."""


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
        self.yandex_search_url = os.getenv(
            "YANDEX_SEARCH_URL",
            "https://searchapi.api.cloud.yandex.net/v2/web/search",
        ).rstrip("/")
        self.yandex_search_api_key = os.getenv("YANDEX_SEARCH_API_KEY", "").strip()
        self.yandex_search_folder_id = os.getenv(
            "YANDEX_SEARCH_FOLDER_ID", ""
        ).strip()
        self.yandex_search_timeout = float(
            os.getenv("YANDEX_SEARCH_TIMEOUT_SECONDS", "20")
        )


settings = Settings()


class DialogMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime


class DialogResponse(BaseModel):
    code: str
    messages: list[DialogMessage]


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class SendMessageResponse(DialogResponse):
    reply: str


class Bookmark(BaseModel):
    id: str
    name: str
    icon: str
    prompt_file: str | None = None
    created_at: datetime


class BookmarksState(BaseModel):
    bookmarks: list[Bookmark]
    active_bookmark: str = "default"
    last_created_at: datetime | None = None


class BookmarksResponse(BaseModel):
    bookmarks: list[Bookmark]
    active: str
    thought: str | None = None


class CreateBookmarkRequest(BaseModel):
    description: str = Field(min_length=3, max_length=1200)


class CreateBookmarkResponse(BookmarksResponse):
    bookmark: Bookmark


class ActivateBookmarkResponse(BookmarksResponse):
    messages: list[DialogMessage]


class DeleteBookmarkResponse(BookmarksResponse):
    deleted: bool


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
        return self.directory / code / "dialog.json"

    def _legacy_path_for(self, code: str) -> Path:
        return self.directory / f"{code}.json"

    def _bookmarks_path_for(self, code: str) -> Path:
        return self.directory / code / "bookmarks.json"

    def _prompts_dir_for(self, code: str) -> Path:
        return self.directory / code / "prompts"

    def load(self, code: str) -> list[DialogMessage]:
        path = self._path_for(code)
        if not path.exists():
            path = self._legacy_path_for(code)
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
            path.parent.mkdir(parents=True, exist_ok=True)
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

    def load_bookmarks(self, code: str) -> BookmarksState:
        path = self._bookmarks_path_for(code)
        if not path.exists():
            return BookmarksState(
                bookmarks=[
                    Bookmark(
                        id="default",
                        name="По умолчанию",
                        icon="🎭",
                        created_at=datetime.now(UTC),
                    )
                ]
            )

        try:
            state = BookmarksState.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if not any(bookmark.id == "default" for bookmark in state.bookmarks):
                state.bookmarks.insert(
                    0,
                    Bookmark(
                        id="default",
                        name="По умолчанию",
                        icon="🎭",
                        created_at=datetime.now(UTC),
                    ),
                )
            return state
        except (OSError, TypeError, ValueError) as error:
            logger.exception("Could not read bookmarks for %s: %s", code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось прочитать список собеседников.",
            ) from error

    def save_bookmarks(self, code: str, state: BookmarksState) -> None:
        path = self._bookmarks_path_for(code)
        temporary_path = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(
                    state.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary_path.replace(path)
        except OSError as error:
            logger.exception("Could not save bookmarks for %s: %s", code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось сохранить список собеседников.",
            ) from error

    def save_prompt(self, code: str, filename: str, content: str) -> str:
        prompts_dir = self._prompts_dir_for(code)
        path = prompts_dir / filename
        try:
            prompts_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as error:
            logger.exception("Could not save prompt for %s: %s", code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось сохранить нового собеседника.",
            ) from error
        return str(Path("prompts") / filename)

    def load_prompt(self, code: str, prompt_file: str) -> str:
        path = self.directory / code / prompt_file
        try:
            if not path.is_file() or path.parent != self._prompts_dir_for(code):
                raise FileNotFoundError(prompt_file)
            return path.read_text(encoding="utf-8").strip()
        except OSError as error:
            logger.exception("Could not read prompt %s for %s: %s", prompt_file, code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось прочитать настройки собеседника.",
            ) from error

    def delete_prompt(self, code: str, prompt_file: str) -> None:
        path = self.directory / code / prompt_file
        try:
            if path.parent == self._prompts_dir_for(code):
                path.unlink(missing_ok=True)
        except OSError as error:
            logger.exception("Could not delete prompt %s for %s: %s", prompt_file, code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось удалить собеседника.",
            ) from error


class MissingOpenAIKeyError(RuntimeError):
    pass


class MissingYandexSearchConfigError(RuntimeError):
    pass


class OpenAIClient:
    async def complete(
        self,
        messages: list[DialogMessage],
        system_prompt: str | None = None,
    ) -> str:
        if not settings.openai_api_key:
            raise MissingOpenAIKeyError

        request_messages = [
            {
                "role": "system",
                "content": system_prompt or settings.system_prompt,
            },
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


class YandexSearchClient:
    async def search(self, query: str) -> str:
        if (
            not settings.yandex_search_api_key
            or not settings.yandex_search_folder_id
        ):
            raise MissingYandexSearchConfigError

        try:
            async with httpx.AsyncClient(
                timeout=settings.yandex_search_timeout
            ) as client:
                response = await client.post(
                    settings.yandex_search_url,
                    headers={
                        "Authorization": f"Api-Key {settings.yandex_search_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "query": {
                            "searchType": "SEARCH_TYPE_RU",
                            "queryText": query,
                            "familyMode": "FAMILY_MODE_MODERATE",
                            "page": "0",
                            "fixTypoMode": "FIX_TYPO_MODE_ON",
                        },
                        "sortSpec": {"sortMode": "SORT_MODE_BY_RELEVANCE"},
                        "groupSpec": {
                            "groupMode": "GROUP_MODE_FLAT",
                            "groupsOnPage": "5",
                            "docsInGroup": "1",
                        },
                        "maxPassages": "2",
                        "folderId": settings.yandex_search_folder_id,
                        "l10n": "LOCALIZATION_RU",
                        "responseFormat": "FORMAT_XML",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as error:
            logger.exception("Yandex Search request failed: %s", error)
            raise HTTPException(
                status_code=502,
                detail="Не удалось найти справочную информацию для нового собеседника.",
            ) from error

        raw_data = payload.get("rawData", "")
        if not isinstance(raw_data, str) or not raw_data.strip():
            raise HTTPException(
                status_code=502,
                detail="Поиск не вернул справочную информацию.",
            )
        return extract_search_context(raw_data)


def clean_markup(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def extract_search_context(raw_data: str) -> str:
    urls = re.findall(r"<url[^>]*>(.*?)</url>", raw_data, flags=re.IGNORECASE | re.DOTALL)
    titles = re.findall(
        r"<title[^>]*>(.*?)</title>", raw_data, flags=re.IGNORECASE | re.DOTALL
    )
    snippets = re.findall(
        r"<(?:passage|snippet)[^>]*>(.*?)</(?:passage|snippet)>",
        raw_data,
        flags=re.IGNORECASE | re.DOTALL,
    )

    entries: list[str] = []
    for index, url in enumerate(urls[:5]):
        title = clean_markup(titles[index]) if index < len(titles) else ""
        snippet = clean_markup(snippets[index]) if index < len(snippets) else ""
        cleaned_url = clean_markup(url)
        parts = [part for part in (title, snippet, cleaned_url) if part]
        if parts:
            entries.append(" — ".join(parts))

    if entries:
        return "\n".join(f"- {entry}" for entry in entries)[:12000]
    return clean_markup(raw_data)[:12000]


def parse_json_object(raw_text: str) -> dict[str, object]:
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise HTTPException(
            status_code=502,
            detail="Не удалось разобрать ответ при создании собеседника.",
        )
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=502,
            detail="Не удалось разобрать ответ при создании собеседника.",
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="Ответ при создании собеседника имеет неверный формат.",
        )
    return payload


def prompt_word_count(prompt: str) -> int:
    return len(re.findall(r"\S+", prompt))


def active_prompt(code: str, state: BookmarksState) -> str:
    bookmark = next(
        (item for item in state.bookmarks if item.id == state.active_bookmark),
        None,
    )
    if bookmark is None or bookmark.id == "default" or not bookmark.prompt_file:
        return settings.system_prompt
    return store.load_prompt(code, bookmark.prompt_file)


def bookmarks_response(
    state: BookmarksState,
    thought: str | None = None,
) -> BookmarksResponse:
    return BookmarksResponse(
        bookmarks=state.bookmarks,
        active=state.active_bookmark,
        thought=thought,
    )


async def generate_character_thought(code: str, state: BookmarksState) -> str:
    if state.active_bookmark == "default" or not settings.openai_api_key:
        return DEFAULT_DAILY_THOUGHT

    character_prompt = active_prompt(code, state)
    try:
        return await openai_client.complete(
            [
                DialogMessage(
                    id="thought-request",
                    role="user",
                    content="Сгенерируй короткую мудрую мысль в 1–2 предложениях в стиле этого собеседника. Не добавляй кавычки, пояснения или заголовок.",
                    created_at=datetime.now(UTC),
                )
            ],
            system_prompt=character_prompt,
        )
    except MissingOpenAIKeyError:
        return DEFAULT_DAILY_THOUGHT


async def moderate_description(description: str) -> None:
    moderation_prompt = """Ты — строгий модератор описаний персонажей для безопасного дружеского чата.
Проверь описание на просьбы романтизировать или поощрять алкоголь, наркотики, насилие,
суицид или самоповреждение. Верни только одно слово: ALLOW или REJECT.
Безопасные упоминания исторических фактов и просьбы мягко отговаривать от опасного поведения разрешены."""
    try:
        result = await openai_client.complete(
            [
                DialogMessage(
                    id="moderation-request",
                    role="user",
                    content=description,
                    created_at=datetime.now(UTC),
                )
            ],
            system_prompt=moderation_prompt,
        )
    except MissingOpenAIKeyError as error:
        raise HTTPException(
            status_code=503,
            detail="Для создания собеседника нужно настроить OpenAI API.",
        ) from error

    if result.strip().upper().startswith("REJECT"):
        raise HTTPException(
            status_code=400,
            detail="Это описание не подходит. Попробуйте другого персонажа — писателя, актёра или историческую фигуру.",
        )


async def generate_character_prompt(
    description: str,
    search_context: str,
) -> tuple[str, str, str]:
    generation_prompt = f"""Создай system prompt для безопасного ИИ-собеседника.
Описание пользователя:
{description}

Справочный контекст из поиска:
{search_context}

Верни только JSON без markdown-обёртки:
{{
  "name": "короткое имя персонажа на русском",
  "icon": "один подходящий символ-иконка",
  "prompt": "system prompt на русском"
}}

System prompt должен быть 200–300 слов: опиши роль, тон, стиль речи и темы разговора.
Не выдавай себя за реального человека и не утверждай, что у тебя есть личные воспоминания.
Не копируй длинные фрагменты произведений. Максимальный размер prompt — 500 слов."""
    try:
        raw_result = await openai_client.complete(
            [
                DialogMessage(
                    id="character-generation-request",
                    role="user",
                    content=generation_prompt,
                    created_at=datetime.now(UTC),
                )
            ],
            system_prompt="Ты аккуратный редактор системных инструкций для дружеского чата.",
        )
    except MissingOpenAIKeyError as error:
        raise HTTPException(
            status_code=503,
            detail="Для создания собеседника нужно настроить OpenAI API.",
        ) from error

    payload = parse_json_object(raw_result)
    name = str(payload.get("name", "")).strip()[:80]
    icon = str(payload.get("icon", "✨")).strip()[:4] or "✨"
    prompt = str(payload.get("prompt", "")).strip()
    if not name or not prompt:
        raise HTTPException(
            status_code=502,
            detail="Модель не вернула имя и настройки нового собеседника.",
        )
    if prompt_word_count(prompt) > MAX_PROMPT_WORDS:
        raise HTTPException(
            status_code=502,
            detail="Настройки нового собеседника получились слишком длинными.",
        )
    if "## Общая безопасность" not in prompt:
        prompt = f"{prompt}\n\n{UNIVERSAL_CHARACTER_SAFETY}"
    return name, icon, prompt


store = SessionStore(settings.sessions_dir)
openai_client = OpenAIClient()
yandex_search_client = YandexSearchClient()


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
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.get("/api/healthz")
async def healthz() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "openai_configured": bool(settings.openai_api_key),
        "yandex_search_configured": bool(
            settings.yandex_search_api_key and settings.yandex_search_folder_id
        ),
    }


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


@app.get(
    "/api/bookmarks/{code}",
    response_model=BookmarksResponse,
)
async def get_bookmarks(
    code: str = PathParam(...),
) -> BookmarksResponse:
    normalized_code = normalize_code(code)
    async with store.lock_for(normalized_code):
        state = store.load_bookmarks(normalized_code)
        thought = await generate_character_thought(normalized_code, state)
    return bookmarks_response(state, thought)


@app.post(
    "/api/bookmarks/{code}",
    response_model=CreateBookmarkResponse,
)
async def create_bookmark(
    request: CreateBookmarkRequest,
    code: str = PathParam(...),
) -> CreateBookmarkResponse:
    normalized_code = normalize_code(code)
    description = request.description.strip()
    if len(description) < 3:
        raise HTTPException(
            status_code=422,
            detail="Опишите нового собеседника чуть подробнее.",
        )

    async with store.lock_for(normalized_code):
        state = store.load_bookmarks(normalized_code)
        if len(state.bookmarks) >= MAX_BOOKMARKS:
            raise HTTPException(
                status_code=400,
                detail="Можно создать не больше десяти собеседников.",
            )

        now = datetime.now(UTC)
        if state.last_created_at:
            seconds_since_creation = (
                now - state.last_created_at
            ).total_seconds()
            if seconds_since_creation < BOOKMARK_CREATION_COOLDOWN_SECONDS:
                wait_seconds = max(
                    1,
                    int(BOOKMARK_CREATION_COOLDOWN_SECONDS - seconds_since_creation),
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Новый собеседник будет доступен через {wait_seconds} сек.",
                )

        await moderate_description(description)
        search_context = await yandex_search_client.search(
            f"{description} биография стиль речи произведения"
        )
        name, icon, prompt = await generate_character_prompt(
            description,
            search_context,
        )

        index = 1
        used_files = {
            bookmark.prompt_file
            for bookmark in state.bookmarks
            if bookmark.prompt_file
        }
        while f"prompts/персонаж_{index:02d}.md" in used_files:
            index += 1

        bookmark = Bookmark(
            id=f"character-{index:02d}",
            name=name,
            icon=icon,
            prompt_file=f"prompts/персонаж_{index:02d}.md",
            created_at=now,
        )
        prompt_markdown = f"# {name}\n\n{prompt}\n"
        store.save_prompt(
            normalized_code,
            f"персонаж_{index:02d}.md",
            prompt_markdown,
        )
        state.bookmarks.append(bookmark)
        state.last_created_at = now
        store.save_bookmarks(normalized_code, state)

    return CreateBookmarkResponse(
        **bookmarks_response(state, DEFAULT_DAILY_THOUGHT).model_dump(),
        bookmark=bookmark,
    )


@app.put(
    "/api/bookmarks/{code}/{bookmark_id}",
    response_model=ActivateBookmarkResponse,
)
async def activate_bookmark(
    bookmark_id: str,
    code: str = PathParam(...),
) -> ActivateBookmarkResponse:
    normalized_code = normalize_code(code)
    async with store.lock_for(normalized_code):
        state = store.load_bookmarks(normalized_code)
        target = next(
            (bookmark for bookmark in state.bookmarks if bookmark.id == bookmark_id),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=404,
                detail="Такого собеседника нет в этом разговоре.",
            )

        next_state = state.model_copy(update={"active_bookmark": target.id})
        thought = await generate_character_thought(normalized_code, next_state)
        messages = store.load(normalized_code)
        if state.active_bookmark != target.id:
            messages.append(
                DialogMessage(
                    id=f"system-{datetime.now(UTC).timestamp()}",
                    role="system",
                    content=f'Собеседник сменился на «{target.name}». Продолжайте разговор.',
                    created_at=datetime.now(UTC),
                )
            )
            store.save(normalized_code, messages)
        store.save_bookmarks(normalized_code, next_state)

    return ActivateBookmarkResponse(
        **bookmarks_response(next_state, thought).model_dump(),
        messages=messages,
    )


@app.delete(
    "/api/bookmarks/{code}/{bookmark_id}",
    response_model=DeleteBookmarkResponse,
)
async def delete_bookmark(
    bookmark_id: str,
    code: str = PathParam(...),
) -> DeleteBookmarkResponse:
    normalized_code = normalize_code(code)
    async with store.lock_for(normalized_code):
        state = store.load_bookmarks(normalized_code)
        target = next(
            (bookmark for bookmark in state.bookmarks if bookmark.id == bookmark_id),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=404,
                detail="Такого собеседника нет в этом разговоре.",
            )
        if target.id == "default":
            raise HTTPException(
                status_code=400,
                detail="Собеседника «По умолчанию» удалить нельзя.",
            )
        if state.active_bookmark == target.id:
            raise HTTPException(
                status_code=409,
                detail="Сначала переключитесь на другого собеседника.",
            )

        state.bookmarks = [
            bookmark for bookmark in state.bookmarks if bookmark.id != target.id
        ]
        store.save_bookmarks(normalized_code, state)
        if target.prompt_file:
            store.delete_prompt(normalized_code, target.prompt_file)

    return DeleteBookmarkResponse(
        **bookmarks_response(state, DEFAULT_DAILY_THOUGHT).model_dump(),
        deleted=True,
    )


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
        message_text = request.message.strip()
        if not message_text:
            raise HTTPException(
                status_code=422,
                detail="Сообщение не должно быть пустым.",
            )
        bookmark_state = store.load_bookmarks(normalized_code)
        existing_messages = store.load(normalized_code)
        user_message = DialogMessage(
            id=f"user-{datetime.now(UTC).timestamp()}",
            role="user",
            content=message_text,
            created_at=datetime.now(UTC),
        )
        context = [*existing_messages, user_message]

        try:
            reply = await openai_client.complete(
                context,
                system_prompt=active_prompt(normalized_code, bookmark_state),
            )
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