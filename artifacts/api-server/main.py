from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("dialog")

CODE_RE = re.compile(r"^[A-Za-z0-9]{6}$")
MAX_PROMPT_WORDS = 500
BOOKMARK_CREATION_COOLDOWN_SECONDS = 60
# Сессия создания собеседника: через это время после нажатия «Добавить нового»
# режим создания гаснет и агент возвращается к обычному разговору.
CREATOR_SESSION_TIMEOUT_SECONDS = 15 * 60
DEFAULT_BOOKMARK_ID = "default"
DEFAULT_BOOKMARK_NAME = "Просто спросить"
DEFAULT_BOOKMARK_ICON = "🌿"
MAX_HISTORY_MESSAGES = 60
# Сжатие давней истории: первая сводка создаётся, когда история собеседника
# достигает SUMMARY_THRESHOLD_MESSAGES сообщений; дальше сводка обновляется
# раз в SUMMARY_STEP_MESSAGES новых сообщений. Сводка хранится в summaries.json
# и подставляется в system prompt, саму историю она не укорачивает.
SUMMARY_THRESHOLD_MESSAGES = 20
SUMMARY_STEP_MESSAGES = 20
SUMMARY_CHUNK_LIMIT = 120
SUMMARY_MESSAGE_SNIPPET_LIMIT = 1200
MAX_SEARCH_ROUNDS = 2
MAX_SEARCH_SNIPPETS = 5

CHAT_TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Поиск в интернете. Вызывай, когда собеседник спрашивает о фактах, "
                "новостях, погоде, цене, расписании, биографии или другом, что могло "
                "измениться или о чём ты не знаешь наверняка."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос на русском языке",
                    },
                },
                "required": ["query"],
            },
        },
    }
]

# Агент-создатель собеседников пользуется тем же инструментом поиска.
CREATOR_TOOLS = CHAT_TOOLS

CREATOR_STATUSES = {
    "chatter",
    "created",
    "clarification_required",
    "rejected",
    "not_found",
}

PROMPTS_DIR = Path(
    os.getenv("DIALOG_PROMPTS_DIR", "data/prompts")
)

# Собранный фронтенд раздаётся тем же процессом FastAPI (один порт, один origin).
# Перед деплоем собрать: PORT=8011 BASE_PATH=/ pnpm --filter @workspace/dialog run build
DIALOG_UI_DIST_DIR = Path(
    os.getenv("DIALOG_UI_DIST_DIR", "dialog/dist/public")
)

GREETING_MORNING_CUTOFF = 11
GREETING_EVENING_CUTOFF = 17

WEATHER_PRESETS: dict[int, dict[str, str]] = {
    0: {"summary": "Ясно"},
    1: {"summary": "Ясно"},
    2: {"summary": "Переменная облачность"},
    3: {"summary": "Пасмурно"},
    45: {"summary": "Туман"},
    48: {"summary": "Туман"},
    51: {"summary": "Морось"},
    53: {"summary": "Морось"},
    55: {"summary": "Морось"},
    56: {"summary": "Ледяная морось"},
    57: {"summary": "Ледяная морось"},
    61: {"summary": "Дождь"},
    63: {"summary": "Дождь"},
    65: {"summary": "Дождь"},
    66: {"summary": "Ледяной дождь"},
    67: {"summary": "Ледяной дождь"},
    71: {"summary": "Снег"},
    73: {"summary": "Снег"},
    75: {"summary": "Снег"},
    77: {"summary": "Снежные зёрна"},
    80: {"summary": "Ливень"},
    81: {"summary": "Ливень"},
    82: {"summary": "Ливень"},
    85: {"summary": "Снегопад"},
    86: {"summary": "Снегопад"},
    95: {"summary": "Гроза"},
    96: {"summary": "Гроза"},
    99: {"summary": "Гроза"},
}

SAMARA_PLACE = "Самара"
SAMARA_LATITUDE = os.getenv("WEATHER_LATITUDE", "53.195873")
SAMARA_LONGITUDE = os.getenv("WEATHER_LONGITUDE", "50.100193")
SAMARA_TIMEZONE = os.getenv("WEATHER_TIMEZONE", "Europe/Samara")

HOROSCOPE_SEARCH_QUERY = "гороскоп на сегодня"

# Прогноз на день без поисковой сводки (поиск не настроен или недоступен).
HOROSCOPE_WITHOUT_SEARCH_PROMPT = """Ты — тёплый неспешный собеседник для пожилого человека.
Напиши «прогноз на день» из 1–3 коротких предложений: по-доброму, с лёгким юмором
и заботой. Один раз естественно упомяни быт или домашнее дело — например, приготовить
курочку в духовке, сходить в магазин, полить цветы или почитать старую книгу.
Не обещай денежных выигрышей и не упоминай слова «гороскоп» и «знак зодиака».
Верни только текст без кавычек, заголовков и пояснений."""

DEFAULT_SYSTEM_PROMPT = """Ты — доброжелательный собеседник для пожилого человека.
Говори по-русски, тепло и простыми словами. Отвечай коротко, без сложных терминов,
и задавай один естественный открытый вопрос, когда это уместно.
Не осуждай и не читай нотации. Если человек сообщает об угрозе жизни, тяжёлой
абстиненции, суицидальных мыслях или резком ухудшении здоровья, спокойно посоветуй
обратиться к близкому человеку или вызвать скорую помощь. Не изображай врача.
Человек находится в Самаре: учитывай это, говоря о времени, погоде и местной жизни,
и не выдумывай значения погоды — если знаешь точную температуру из данных, называй её."""
DEFAULT_DAILY_THOUGHT = "«Хороший разговор — это тоже прогулка»"
UNIVERSAL_CHARACTER_SAFETY = """## Общая безопасность
- Не поощряй алкоголь, наркотики, насилие или самоповреждение.
- Если разговор касается алкоголя или наркотиков, мягко отговаривай и предлагай безопасную альтернативу.
- Не романтизируй зависимость, насилие, суицид или раннюю смерть.
- Если человек сообщает об угрозе жизни или резком ухудшении здоровья, спокойно посоветуй обратиться к близкому человеку, врачу или вызвать скорую помощь. объясни почему это важно."""

# Резервный промпт агента-создателя, если data/prompts/creator.md недоступен.
CREATOR_FALLBACK_PROMPT = """Ты — помощник в общем диалоге с пожилым человеком.
У тебя две роли, и ты сам выбираешь роль по сообщению собеседника: тёплый
собеседник, если человек хочет просто поговорить, и создатель собеседников,
если человек хочет, чтобы ты создал нового ИИ-собеседника. Ты не притворяешься
персонажем, которого создаёшь.

Всегда отвечай ровно одним JSON-объектом без Markdown-обёртки:
- обычный разговор: {"status": "chatter", "user_message": "<твой тёплый ответ>"}
- собеседник создан: {"status": "created", "name": "<короткое имя>", "icon": "<эмодзи>", "user_message": "<1-3 тёплых предложения>", "prompt_markdown": "<готовый системный промпт собеседника, 150-350 слов>"}
- нужно уточнение: {"status": "clarification_required", "user_message": "<один-два коротких вопроса>"}
- создать нельзя: {"status": "rejected", "user_message": "<мягкий отказ и безопасная альтернатива>"}
- человека не удалось найти: {"status": "not_found", "user_message": "<попроси уточнить имя или рассказать, чем он известен>"}

Главное правило выбора роли: собеседника создают только «с кем поговорить»,
а не «о чём поговорить». Вопрос о вещи или предмете (микросхема, микрофон,
погода, ремонт) — это обычный разговор (chatter), даже если предмет
технический или незнакомый тебе. Не выдумывай собеседника-«специалиста» из
темы вопроса. Обобщённые образы вроде «старого рыбака» создаёшь, только если
человек просит именно собеседника в таком образе.

Можно создавать: публичных исторических личностей и умерших деятелей культуры,
вымышленных персонажей, обобщённые образы (старый рыбак, сельский учитель,
сосед по даче) и собеседников, описанных через манеру разговора. Для живых
публичных людей создавай условную художественную интерпретацию, а не копию
личности. Нельзя создавать собеседника от имени родных, знакомых и частных
лиц — предложи помочь вспомнить человека, поговорить о нём или составить
сообщение. Не создавай «собутыльника» и персонажей, подталкивающих к выпивке.

Если названа публичная личность — используй инструмент search, чтобы проверить
имя, годы жизни, род деятельности и произведения. Не включай в промпт слухи и
выдуманные факты. Если человека не удалось надёжно найти — верни not_found.

Говори простыми словами, короткими абзацами, без нотаций. При обычном разговоре
задавай один естественный открытый вопрос, когда это уместно. Не осуждай и не
читай нотации. Если человек сообщает об угрозе жизни или тяжёлом состоянии,
спокойно посоветуй обратиться к близкому человеку или вызвать скорую помощь."""

CREATOR_SAVED_NOTE = (
    "Готово! Новый собеседник «{name}» появился в списке справа. "
    "Наш прежний разговор никуда не делся — можно вернуться и продолжить."
)
CREATOR_SWITCHED_NOTE = (
    "Собеседник сменился на «{name}». Наш прежний разговор сохранён — "
    "можно вернуться в любой момент."
)

SUMMARIZE_FALLBACK_PROMPT = """Ты ведёшь краткую сводку давней части разговора между человеком и его собеседником.
Тебе дают предыдущую сводку (если она была) и новую часть диалога. Обнови сводку так,
чтобы она отражала всё важное: темы и события, факты о человеке (имя, семья, здоровье,
настроение, планы), о чём он просил и что решил.
Пиши по-русски, от третьего лица, нейтрально и тепло, не длиннее 150 слов.
В ответе — только текст сводки, без заголовков, приветствий и пояснений."""

_PROMPT_FILE_CACHE: dict[str, str] = {}


def load_prompt_file(filename: str) -> str | None:
    """Читает промпт из data/prompts/<filename>; None, если файла нет."""
    if filename in _PROMPT_FILE_CACHE:
        return _PROMPT_FILE_CACHE[filename] or None
    path = PROMPTS_DIR / filename
    try:
        content = path.read_text(encoding="utf-8").strip() or None
    except OSError as error:
        logger.warning("Could not read prompt file %s: %s", path, error)
        content = None
    _PROMPT_FILE_CACHE[filename] = content or ""
    return content


class Settings:
    def __init__(self) -> None:
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_base_url = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        ).rstrip("/")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        self.openai_timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
        # Приоритет: переменная окружения -> data/prompts/default.md -> константа.
        self.system_prompt = (
            os.getenv("DIALOG_SYSTEM_PROMPT", "").strip()
            or load_prompt_file("default.md")
            or DEFAULT_SYSTEM_PROMPT
        )
        self.character_safety_prompt = (
            load_prompt_file("character_safety.md")
            or UNIVERSAL_CHARACTER_SAFETY
        )
        self.sessions_dir = Path(
            os.getenv("DIALOG_SESSIONS_DIR", "data/sessions")
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
        self.horoscope_search_timeout = float(
            os.getenv("HOROSCOPE_SEARCH_TIMEOUT_SECONDS", "20")
        )
        self.prompts_dir = PROMPTS_DIR
        self.dialog_ui_dist_dir = DIALOG_UI_DIST_DIR


settings = Settings()


class DialogMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime


class DialogResponse(BaseModel):
    code: str
    messages: list[DialogMessage]


class Greeting(BaseModel):
    text: str
    time_of_day: Literal["утро", "день", "вечер", "ночь"]


class WeatherResponse(BaseModel):
    temperature: float
    summary: str
    place: str
    updated_at: datetime


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class Bookmark(BaseModel):
    id: str
    name: str
    icon: str
    prompt_file: str | None = None
    created_at: datetime


class SendMessageResponse(DialogResponse):
    reply: str
    created_bookmark: Bookmark | None = None


class BookmarksState(BaseModel):
    bookmarks: list[Bookmark]
    active_bookmark: str = "default"
    last_created_at: datetime | None = None
    # Момент нажатия «Добавить нового»: включает режим создания на
    # CREATOR_SESSION_TIMEOUT_SECONDS. None — режим создания выключен.
    creator_started_at: datetime | None = None


class DialogSummary(BaseModel):
    text: str
    message_count: int = 0
    updated_at: datetime


class SummariesState(BaseModel):
    summaries: dict[str, DialogSummary] = Field(default_factory=dict)


class BookmarksResponse(BaseModel):
    bookmarks: list[Bookmark]
    active: str
    thought: str | None = None


class HoroscopeResponse(BaseModel):
    horoscope: str


class CreateBookmarkRequest(BaseModel):
    description: str = Field(min_length=3, max_length=1200)


class CreateBookmarkResponse(BookmarksResponse):
    bookmark: Bookmark


class ActivateBookmarkResponse(BookmarksResponse):
    messages: list[DialogMessage]


class DeleteBookmarkResponse(BookmarksResponse):
    deleted: bool


class CreatorOutcome(NamedTuple):
    status: str
    user_message: str
    name: str = ""
    icon: str = ""
    prompt_markdown: str = ""


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

    def _actor_path_for(self, code: str, bookmark_id: str) -> Path:
        return self.directory / code / f"dialog_{bookmark_id}.json"

    def _summaries_path_for(self, code: str) -> Path:
        return self.directory / code / "summaries.json"

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

    def load_actor_history(self, code: str, bookmark_id: str) -> list[DialogMessage]:
        """История конкретного собеседника; пустой список, если её ещё нет."""
        if bookmark_id == DEFAULT_BOOKMARK_ID:
            return self.load(code)
        path = self._actor_path_for(code, bookmark_id)
        if not path.exists():
            return []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [DialogMessage.model_validate(item) for item in payload["messages"]]
        except (OSError, KeyError, TypeError, ValueError) as error:
            logger.exception(
                "Could not read history %s for %s: %s", bookmark_id, code, error
            )
            raise HTTPException(
                status_code=500,
                detail="Не удалось прочитать историю этого разговора.",
            ) from error

    def save_actor_history(
        self,
        code: str,
        bookmark_id: str,
        messages: list[DialogMessage],
    ) -> None:
        if bookmark_id == DEFAULT_BOOKMARK_ID:
            self.save(code, messages)
            return
        path = self._actor_path_for(code, bookmark_id)
        temporary_path = path.with_suffix(".json.tmp")
        payload = {
            "code": code,
            "bookmark_id": bookmark_id,
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
            logger.exception(
                "Could not save history %s for %s: %s", bookmark_id, code, error
            )
            raise HTTPException(
                status_code=500,
                detail="Не удалось сохранить историю разговора.",
            ) from error

    def delete_actor_history(self, code: str, bookmark_id: str) -> None:
        if bookmark_id == DEFAULT_BOOKMARK_ID:
            return
        path = self._actor_path_for(code, bookmark_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            logger.exception(
                "Could not delete history %s for %s: %s", bookmark_id, code, error
            )

    def load_summaries(self, code: str) -> SummariesState:
        path = self._summaries_path_for(code)
        if not path.exists():
            return SummariesState()

        try:
            return SummariesState.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, TypeError, ValueError) as error:
            logger.exception("Could not read summaries for %s: %s", code, error)
            return SummariesState()

    def save_summaries(self, code: str, state: SummariesState) -> None:
        path = self._summaries_path_for(code)
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
            logger.exception("Could not save summaries for %s: %s", code, error)

    def delete_actor_summary(self, code: str, bookmark_id: str) -> None:
        state = self.load_summaries(code)
        if bookmark_id not in state.summaries:
            return
        del state.summaries[bookmark_id]
        self.save_summaries(code, state)

    def load_bookmarks(self, code: str) -> BookmarksState:
        path = self._bookmarks_path_for(code)
        if not path.exists():
            return BookmarksState(
                bookmarks=[
                    self._default_bookmark()
                ]
            )

        try:
            state = BookmarksState.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if not any(bookmark.id == DEFAULT_BOOKMARK_ID for bookmark in state.bookmarks):
                state.bookmarks.insert(0, self._default_bookmark())
            return state
        except (OSError, TypeError, ValueError) as error:
            logger.exception("Could not read bookmarks for %s: %s", code, error)
            raise HTTPException(
                status_code=500,
                detail="Не удалось прочитать список собеседников.",
            ) from error

    @staticmethod
    def _default_bookmark() -> Bookmark:
        return Bookmark(
            id=DEFAULT_BOOKMARK_ID,
            name=DEFAULT_BOOKMARK_NAME,
            icon=DEFAULT_BOOKMARK_ICON,
            created_at=datetime.now(UTC),
        )

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
    async def _chat(
        self,
        request_messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": settings.openai_model,
            "messages": request_messages,
            "temperature": 0.7,
        }
        if tools:
            body["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=settings.openai_timeout) as client:
                response = await client.post(
                    f"{settings.openai_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as error:
            logger.exception("OpenAI request failed: %s", error)
            raise HTTPException(
                status_code=502,
                detail="Собеседник сейчас не отвечает. Попробуйте ещё раз.",
            ) from error

    async def chat(
        self,
        request_messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        payload = await self._chat(request_messages, tools=tools)
        choices = payload.get("choices")
        message = (
            choices[0].get("message", {})
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else {}
        )
        if not isinstance(message, dict) or not message:
            logger.error("Unexpected OpenAI response shape")
            raise HTTPException(
                status_code=502,
                detail="Собеседник вернул неполный ответ. Попробуйте ещё раз.",
            )
        return message

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
        message = await self.chat(request_messages)

        reply = message.get("content")
        if not isinstance(reply, str) or not reply.strip():
            raise HTTPException(
                status_code=502,
                detail="Собеседник вернул пустой ответ. Попробуйте ещё раз.",
            )
        return reply.strip()


    async def complete_raw(
        self,
        request: list[dict[str, str]],
    ) -> dict[str, str]:
        """Плоское дописывание (без инструментов); возвращает content/role."""
        if not settings.openai_api_key:
            raise MissingOpenAIKeyError
        message = await self.chat(request)
        content = message.get("content")
        return {
            "role": str(message.get("role") or "assistant"),
            "content": content if isinstance(content, str) else "",
        }


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
        try:
            # API v2 отдаёт результат (XML/HTML) в Base64 — см. официальную
            # инструкцию «Декодируйте результат из формата Base64».
            raw_data = base64.b64decode(raw_data).decode("utf-8", errors="replace")
        except (ValueError, TypeError) as error:
            raise HTTPException(
                status_code=502,
                detail="Не удалось разобрать результаты поиска.",
            ) from error
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
    for index, url in enumerate(urls[:MAX_SEARCH_SNIPPETS]):
        title = clean_markup(titles[index]) if index < len(titles) else ""
        snippet = clean_markup(snippets[index]) if index < len(snippets) else ""
        cleaned_url = clean_markup(url)
        parts = [part for part in (title, snippet, cleaned_url) if part]
        if parts:
            entries.append(" — ".join(parts))

    if entries:
        return "\n".join(f"- {entry}" for entry in entries)[:12000]
    return clean_markup(raw_data)[:12000]


class WeatherClient:
    """Открытый API open-meteo без ключей: погода в Самаре."""

    async def fetch_current_weather(self) -> WeatherResponse:
        params = {
            "latitude": SAMARA_LATITUDE,
            "longitude": SAMARA_LONGITUDE,
            "current": "temperature_2m,weather_code",
            "timezone": SAMARA_TIMEZONE,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.exception("Open-Meteo request failed: %s", error)
            raise HTTPException(
                status_code=502,
                detail="Не удалось узнать погоду в Самаре.",
            ) from error

        current = payload.get("current") or {}
        temperature = current.get("temperature_2m")
        weather_code = current.get("weather_code")
        updated_at = current.get("time")
        if not isinstance(temperature, (int, float)) or not isinstance(
            weather_code, int
        ):
            raise HTTPException(
                status_code=502,
                detail="Погода в Самаре вернулась в неожиданном формате.",
            )

        preset = WEATHER_PRESETS.get(
            weather_code, {"summary": "Погода проясняется"}
        )
        return WeatherResponse(
            temperature=float(temperature),
            summary=str(preset["summary"]),
            place=SAMARA_PLACE,
            updated_at=updated_at or datetime.now(UTC),
        )


class HoroscopeClient:
    """Прогноз на день: LLM, а при настроенном поиске — со свежей сводкой."""

    async def fetch_horoscope(self) -> str:
        search_context = await self._search_summary()
        prompt = (
            load_prompt_file("horoscope.md")
            if search_context
            else HOROSCOPE_WITHOUT_SEARCH_PROMPT
        )
        content = search_context or "Напиши добрый прогноз на день для пожилого человека."

        try:
            return await openai_client.complete(
                [
                    DialogMessage(
                        id="horoscope-request",
                        role="user",
                        content=content,
                        created_at=datetime.now(UTC),
                    )
                ],
                system_prompt=prompt or HOROSCOPE_WITHOUT_SEARCH_PROMPT,
            )
        except MissingOpenAIKeyError:
            logger.warning(
                "Horoscope: OPENAI_API_KEY не задан, возвращаю запасной текст."
            )
            return DEFAULT_DAILY_THOUGHT

    async def _search_summary(self) -> str:
        """Свежая сводка из поиска; None, если поиск не настроен или упал."""
        try:
            raw_data = await yandex_search_client.search(HOROSCOPE_SEARCH_QUERY)
        except MissingYandexSearchConfigError:
            return ""
        except HTTPException as error:
            logger.warning(
                "Horoscope: поиск не удался (%s), использую LLM без сводки.",
                error.detail,
            )
            return ""
        return clean_markup(raw_data)[:4000]


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


def creator_mode_active(state: BookmarksState) -> bool:
    """Режим создания собеседника: явная сессия после «Добавить нового».

    Включается только нажатием кнопки «Добавить нового» (см. start_creator_mode)
    и гаснет сам через CREATOR_SESSION_TIMEOUT_SECONDS.
    """
    if state.creator_started_at is None:
        return False
    elapsed = (datetime.now(UTC) - state.creator_started_at).total_seconds()
    return 0 <= elapsed < CREATOR_SESSION_TIMEOUT_SECONDS


def start_creator_mode(state: BookmarksState, code: str) -> BookmarksState:
    """Включает режим создания собеседника и запоминает момент старта."""
    next_state = state.model_copy(update={"creator_started_at": datetime.now(UTC)})
    store.save_bookmarks(code, next_state)
    return next_state


def exit_creator_mode(state: BookmarksState, code: str) -> BookmarksState:
    """Гасит режим создания собеседника (после создания или по таймауту)."""
    if state.creator_started_at is None:
        return state
    next_state = state.model_copy(update={"creator_started_at": None})
    store.save_bookmarks(code, next_state)
    return next_state


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
    moderation_prompt = (
        load_prompt_file("creator_moderation.md")
        or """Ты — строгий модератор описаний персонажей для безопасного дружеского чата.
Проверь описание на просьбы романтизировать или поощрять алкоголь, наркотики, насилие,
суицид или самоповреждение. Верни только одно слово: ALLOW или REJECT.
Безопасные упоминания исторических фактов и просьбы мягко отговаривать от опасного поведения разрешены."""
    )
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
            detail="Новые собеседники пока недоступны.",
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
    generation_template = load_prompt_file("creator_generation.md")
    if generation_template:
        generation_prompt = generation_template.format(
            description=description,
            search_context=search_context,
        )
    else:
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
            detail="Новые собеседники пока недоступны.",
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
        prompt = f"{prompt}\n\n{settings.character_safety_prompt}"
    return name, icon, prompt


store = SessionStore(settings.sessions_dir)
openai_client = OpenAIClient()
yandex_search_client = YandexSearchClient()
weather_client = WeatherClient()
horoscope_client = HoroscopeClient()


def trim_window(messages: list[DialogMessage]) -> list[DialogMessage]:
    """Последние MAX_HISTORY_MESSAGES сообщений — то, что реально видит модель."""
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return list(messages)
    return messages[-MAX_HISTORY_MESSAGES:]


def _summary_entry_text(entry: DialogSummary) -> str:
    updated = entry.updated_at.astimezone(UTC).strftime("%d.%m.%Y")
    return f"Краткое содержание более ранней части разговора (по {updated}):\n{entry.text.strip()}"


def build_system_prompt(system_prompt: str, summary: DialogSummary | None) -> str:
    if summary is None or not summary.text.strip():
        return system_prompt
    return f"{system_prompt}\n\n{_summary_entry_text(summary)}"


def summarize_chunk_text(chunk: list[DialogMessage]) -> str:
    lines = [
        f"[{message.created_at.astimezone(UTC).strftime('%d.%m.%Y %H:%M')}] "
        f"{message.role}: {message.content.strip()}"
        for message in chunk
    ]
    return "\n".join(lines)


def summarize_request_text(
    chunk: list[DialogMessage],
    previous: DialogSummary | None,
) -> str:
    headline = f"Предыдущая сводка:\n{previous.text.strip()}" if previous else None
    conversation = summarize_chunk_text(chunk)
    return "\n\n".join(part for part in (headline, conversation) if part)


def to_llm_messages(
    messages: list[DialogMessage],
    system_prompt: str,
    summary: DialogSummary | None = None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": build_system_prompt(system_prompt, summary)},
        *[
            {"role": message.role, "content": message.content}
            for message in trim_window(messages)
        ],
    ]


async def summarize_turn_after_save(
    code: str,
    bookmark_id: str,
    messages: list[DialogMessage],
) -> None:
    """Фоновая дозапись сводки: сжимаем самый старый «хвост» истории.

    Вызывается после сохранения ответа. Никогда не ломает основной диалог:
    любая ошибка только логируется.

    Первая сводка — на SUMMARY_THRESHOLD_MESSAGES сообщениях, обновление —
    раз в SUMMARY_STEP_MESSAGES новых сообщений после свёрнутого блока.
    """
    if not settings.openai_api_key:
        return
    if len(messages) < SUMMARY_THRESHOLD_MESSAGES:
        return

    previous = store.load_summaries(code).summaries.get(bookmark_id)
    already_summarized = previous.message_count if previous else 0
    new_count = len(messages) - already_summarized
    if new_count < SUMMARY_STEP_MESSAGES:
        return

    chunk = messages[:SUMMARY_CHUNK_LIMIT]
    prompt = load_prompt_file("summarize.md") or SUMMARIZE_FALLBACK_PROMPT
    request: list[dict[str, str]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": summarize_request_text(chunk, previous)},
    ]
    try:
        payload = await openai_client.complete_raw(request)
    except Exception as error:  # noqa: BLE001 - сводка не должна ронять чат
        logger.warning("Could not update summary for %s/%s: %s", code, bookmark_id, error)
        return

    summary_text = str(payload.get("content") or "").strip()
    if not summary_text:
        logger.warning("Empty summary for %s/%s, keeping previous", code, bookmark_id)
        return

    summaries = store.load_summaries(code)
    summaries.summaries[bookmark_id] = DialogSummary(
        text=summary_text,
        message_count=len(chunk),
        updated_at=datetime.now(UTC),
    )
    store.save_summaries(code, summaries)


def extract_tool_calls(message: dict[str, object]) -> list[dict[str, object]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [call for call in tool_calls if isinstance(call, dict)]


async def execute_search_tool(call: dict[str, object]) -> str:
    function = call.get("function") or {}
    if not isinstance(function, dict):
        return "Не удалось разобрать запрос на поиск."
    arguments_raw = function.get("arguments")
    try:
        arguments = (
            json.loads(arguments_raw)
            if isinstance(arguments_raw, str)
            else dict(arguments_raw or {})
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        logger.warning("Could not parse search tool arguments: %s", error)
        return "Не удалось разобрать запрос на поиск."

    query = str(arguments.get("query", "")).strip()
    if not query:
        return "Не удалось разобрать запрос на поиск."

    logger.info("Search tool query: %s", query)
    return await yandex_search_client.search(query)


async def chat_reply(
    context: list[DialogMessage],
    system_prompt: str,
    summary: DialogSummary | None = None,
) -> str:
    request_messages = to_llm_messages(context, system_prompt, summary)

    message: dict[str, object] = {}
    for _ in range(MAX_SEARCH_ROUNDS):
        message = await openai_client.chat(request_messages, tools=CHAT_TOOLS)
        tool_calls = extract_tool_calls(message)
        if not tool_calls:
            break

        request_messages.append(message)
        for call in tool_calls:
            call_id = str(call.get("id") or "call")
            try:
                result = await execute_search_tool(call)
            except MissingYandexSearchConfigError:
                result = "Поиск в интернете сейчас недоступен."
            except HTTPException as error:
                result = f"Поиск не удался: {error.detail}"
            request_messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": result}
            )

    reply = message.get("content")
    if not isinstance(reply, str) or not reply.strip():
        raise HTTPException(
            status_code=502,
            detail="Собеседник вернул пустой ответ. Попробуйте ещё раз.",
        )
    return reply.strip()


# --------------------------------------------------------------------------
# Агент-создатель собеседников (промпт: data/prompts/creator.md).
# Работает в общем диалоге, пока активен встроенный собеседник.
# --------------------------------------------------------------------------


def parse_creator_outcome(raw_reply: str) -> CreatorOutcome:
    """Разбирает ответ агента; при сбое JSON считается обычным разговором."""
    cleaned = raw_reply.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(cleaned[start : end + 1])
            if isinstance(payload, dict):
                return _creator_outcome_from_payload(payload, cleaned.strip())
        except json.JSONDecodeError as error:
            logger.warning("Creator reply is not valid JSON: %s", error)
    return CreatorOutcome(status="chatter", user_message=cleaned.strip())


def _creator_outcome_from_payload(
    payload: dict[str, object],
    raw_reply: str,
) -> CreatorOutcome:
    status = str(payload.get("status", "chatter")).strip().lower()
    if status not in CREATOR_STATUSES:
        status = "chatter"
    user_message = str(payload.get("user_message", "")).strip()
    name = str(payload.get("name", "")).strip()[:80]
    icon = str(payload.get("icon", "✨")).strip()[:4] or "✨"
    prompt_markdown = str(payload.get("prompt_markdown", "")).strip()

    if status == "created" and (not name or not prompt_markdown):
        # Модель не передала обязательные поля — считаем ответ обычной репликой.
        logger.warning("Creator payload missing name or prompt_markdown")
        status = "chatter"
        prompt_markdown = ""

    if not user_message:
        user_message = (
            raw_reply
            if status == "chatter"
            else "Расскажи, пожалуйста, чуть подробнее, с кем хочешь поговорить."
        )

    return CreatorOutcome(
        status=status,
        user_message=user_message,
        name=name,
        icon=icon,
        prompt_markdown=prompt_markdown,
    )


def creator_system_prompt(state: BookmarksState) -> str:
    base = load_prompt_file("creator.md") or CREATOR_FALLBACK_PROMPT
    existing = ", ".join(
        f"«{bookmark.name}»"
        for bookmark in state.bookmarks
        if bookmark.id != DEFAULT_BOOKMARK_ID
    ) or "пока никаких"
    return (
        f"{base}\n\n"
        "## Контекст этой сессии\n"
        f"- Уже созданные собеседники: {existing}. Не создавай дубликат — "
        "предложи просто переключиться на существующего.\n"
        "- Новый собеседник сохраняется в общий список справа. Прежний диалог "
        "пользователя при этом не пропадает: он остаётся в общем разговоре, и "
        "к нему всегда можно вернуться. Когда собеседник создан, скажи об этом "
        "одним тёплым предложением.\n"
        "- Один запрос на создание за раз: не создавай нескольких собеседников "
        "из одного сообщения. Если просят несколько — предложи начать с одного."
    )


async def creator_reply(
    context: list[DialogMessage],
    system_prompt: str,
    summary: DialogSummary | None = None,
) -> CreatorOutcome:
    request_messages = to_llm_messages(context, system_prompt, summary)

    message: dict[str, object] = {}
    for _ in range(MAX_SEARCH_ROUNDS):
        message = await openai_client.chat(request_messages, tools=CREATOR_TOOLS)
        tool_calls = extract_tool_calls(message)
        if not tool_calls:
            break

        request_messages.append(message)
        for call in tool_calls:
            call_id = str(call.get("id") or "call")
            try:
                result = await execute_search_tool(call)
            except MissingYandexSearchConfigError:
                result = "Поиск в интернете сейчас недоступен."
            except HTTPException as error:
                result = f"Поиск не удался: {error.detail}"
            request_messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": result}
            )

    raw_reply = message.get("content")
    if not isinstance(raw_reply, str) or not raw_reply.strip():
        raise HTTPException(
            status_code=502,
            detail="Собеседник вернул пустой ответ. Попробуйте ещё раз.",
        )
    return parse_creator_outcome(raw_reply.strip())


def _persist_created_bookmark(
    normalized_code: str,
    state: BookmarksState,
    outcome: CreatorOutcome,
) -> Bookmark:
    """Сохраняет промпт нового собеседника в файл и добавляет его в список."""
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

    prompt_markdown = outcome.prompt_markdown.strip()
    if prompt_word_count(prompt_markdown) > MAX_PROMPT_WORDS:
        raise HTTPException(
            status_code=502,
            detail="Настройки нового собеседника получились слишком длинными. Попробуйте ещё раз.",
        )
    if "## Общая безопасность" not in prompt_markdown:
        prompt_markdown = f"{prompt_markdown}\n\n{settings.character_safety_prompt}"

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
        name=outcome.name[:80] or "Новый собеседник",
        icon=outcome.icon[:4] or "✨",
        prompt_file=f"prompts/персонаж_{index:02d}.md",
        created_at=now,
    )
    store.save_prompt(
        normalized_code,
        f"персонаж_{index:02d}.md",
        f"# {bookmark.name}\n\n{prompt_markdown}\n",
    )
    state.bookmarks.append(bookmark)
    state.last_created_at = now
    store.save_bookmarks(normalized_code, state)
    return bookmark


async def handle_creator_turn(
    normalized_code: str,
    context: list[DialogMessage],
    state: BookmarksState,
    summary: DialogSummary | None = None,
) -> tuple[str, Bookmark | None]:
    """Обрабатывает сообщение в режиме агента-создателя."""
    outcome = await creator_reply(context, creator_system_prompt(state), summary)

    if outcome.status != "created":
        return outcome.user_message, None

    bookmark = _persist_created_bookmark(normalized_code, state, outcome)
    # Персонаж готов: гасим режим создания и сразу переключаемся на него,
    # чтобы продолжить разговор уже новым собеседником.
    exit_creator_mode(state, normalized_code)
    state.active_bookmark = bookmark.id
    store.save_bookmarks(normalized_code, state)
    return (
        f"{CREATOR_SAVED_NOTE.format(name=bookmark.name)} {outcome.user_message}",
        bookmark,
    )


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
    if not load_prompt_file("creator.md"):
        logger.warning(
            "Prompt file %s not found — using built-in creator prompt.",
            settings.prompts_dir / "creator.md",
        )
    logger.info(
        "Dialog API started with sessions=%s prompts=%s model=%s openai_configured=%s",
        settings.sessions_dir,
        settings.prompts_dir,
        settings.openai_model,
        bool(settings.openai_api_key),
    )
    yield


app = FastAPI(title="Dialog API", lifespan=lifespan)
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


def greeting_time_of_day(now: datetime) -> Literal["утро", "день", "вечер", "ночь"]:
    hour = now.hour
    if hour < 5:
        return "ночь"
    if hour < GREETING_MORNING_CUTOFF:
        return "утро"
    if hour < GREETING_EVENING_CUTOFF:
        return "день"
    return "вечер"


def build_greeting_text(time_of_day: str) -> str:
    if time_of_day == "утро":
        return "Доброе утро!"
    if time_of_day == "день":
        return "Добрый день!"
    if time_of_day == "вечер":
        return "Добрый вечер!"
    return "Доброй ночи!"


@app.get(
    "/api/greeting",
    response_model=Greeting,
)
async def get_greeting() -> Greeting:
    now = datetime.now(ZoneInfo(SAMARA_TIMEZONE))
    time_of_day = greeting_time_of_day(now)
    return Greeting(text=build_greeting_text(time_of_day), time_of_day=time_of_day)


@app.get(
    "/api/weather",
    response_model=WeatherResponse,
)
async def get_weather() -> WeatherResponse:
    return await weather_client.fetch_current_weather()


@app.get(
    "/api/horoscope",
    response_model=HoroscopeResponse,
)
async def get_horoscope() -> HoroscopeResponse:
    try:
        horoscope = await horoscope_client.fetch_horoscope()
    except MissingYandexSearchConfigError as error:
        raise HTTPException(
            status_code=503,
            detail="Гороскоп пока недоступен: не настроен поиск.",
        ) from error
    except HTTPException:
        raise
    return HoroscopeResponse(horoscope=horoscope)


@app.get(
    "/api/dialog/{code}",
    response_model=DialogResponse,
)
async def get_dialog(
    code: str = PathParam(...),
) -> DialogResponse:
    normalized_code = normalize_code(code)
    async with store.lock_for(normalized_code):
        messages = store.load_actor_history(normalized_code, DEFAULT_BOOKMARK_ID)
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
    """Прямое создание собеседника по описанию (без диалога с агентом).

    Промпт нового собеседника сохраняется в новый файл внутри сессии:
    data/sessions/{code}/prompts/персонаж_NN.md.
    """
    normalized_code = normalize_code(code)
    description = request.description.strip()
    if len(description) < 3:
        raise HTTPException(
            status_code=422,
            detail="Опишите нового собеседника чуть подробнее.",
        )

    async with store.lock_for(normalized_code):
        state = store.load_bookmarks(normalized_code)
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

        bookmark = _persist_created_bookmark(
            normalized_code,
            state,
            CreatorOutcome(
                status="created",
                user_message="",
                name=name,
                icon=icon,
                prompt_markdown=prompt,
            ),
        )

    return CreateBookmarkResponse(
        **bookmarks_response(state, DEFAULT_DAILY_THOUGHT).model_dump(),
        bookmark=bookmark,
    )


@app.post(
    "/api/bookmarks/{code}/creator",
    response_model=BookmarksResponse,
)
async def start_creator(
    code: str = PathParam(...),
) -> BookmarksResponse:
    """Включает режим создания собеседника на CREATOR_SESSION_TIMEOUT_SECONDS."""
    normalized_code = normalize_code(code)
    async with store.lock_for(normalized_code):
        state = store.load_bookmarks(normalized_code)
        state = start_creator_mode(state, normalized_code)
    return bookmarks_response(state, DEFAULT_DAILY_THOUGHT)


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

        # Выбор любого собеседника — выход из режима создания.
        state = exit_creator_mode(state, normalized_code)
        next_state = state.model_copy(update={"active_bookmark": target.id})
        thought = await generate_character_thought(normalized_code, next_state)
        messages = store.load_actor_history(normalized_code, target.id)
        if state.active_bookmark != target.id and target.id != DEFAULT_BOOKMARK_ID:
            messages.append(
                DialogMessage(
                    id=f"system-{datetime.now(UTC).timestamp()}",
                    role="system",
                    content=CREATOR_SWITCHED_NOTE.format(name=target.name),
                    created_at=datetime.now(UTC),
                )
            )
            store.save_actor_history(normalized_code, target.id, messages)
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
        if target.id == DEFAULT_BOOKMARK_ID:
            raise HTTPException(
                status_code=400,
                detail="Собеседника «Просто спросить» удалить нельзя.",
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
        store.delete_actor_history(normalized_code, target.id)
        store.delete_actor_summary(normalized_code, target.id)

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
        is_creator_mode = creator_mode_active(bookmark_state)
        active_bookmark = next(
            (
                bookmark
                for bookmark in bookmark_state.bookmarks
                if bookmark.id == bookmark_state.active_bookmark
            ),
            None,
        )
        # Режим создания живёт в общем диалоге (default). В обычном режиме
        # у каждого собеседника своя история; default без активной сессии
        # создания — обычный разговор со встроенным собеседником.
        if is_creator_mode:
            bookmark_id = DEFAULT_BOOKMARK_ID
        elif active_bookmark is not None and active_bookmark.id != DEFAULT_BOOKMARK_ID:
            bookmark_id = active_bookmark.id
        else:
            bookmark_id = DEFAULT_BOOKMARK_ID
        # У каждого собеседника своя история; в режиме создания работает агент-создатель.
        existing_messages = store.load_actor_history(normalized_code, bookmark_id)
        user_message = DialogMessage(
            id=f"user-{datetime.now(UTC).timestamp()}",
            role="user",
            content=message_text,
            created_at=datetime.now(UTC),
        )
        context = [*existing_messages, user_message]

        created_bookmark: Bookmark | None = None
        actor_summary = store.load_summaries(normalized_code).summaries.get(bookmark_id)
        try:
            if is_creator_mode:
                # Общий диалог ведёт агент-создатель: он болтает, уточняет
                # и создаёт собеседников. Старая история при этом сохраняется.
                reply, created_bookmark = await handle_creator_turn(
                    normalized_code,
                    context,
                    bookmark_state,
                    actor_summary,
                )
            else:
                reply = await chat_reply(
                    context,
                    active_prompt(normalized_code, bookmark_state),
                    actor_summary,
                )
        except MissingOpenAIKeyError as error:
            raise HTTPException(
                status_code=503,
                detail="Ответить пока не получится. Попробуйте ещё раз позже.",
            ) from error

        assistant_message = DialogMessage(
            id=f"assistant-{datetime.now(UTC).timestamp()}",
            role="assistant",
            content=reply,
            created_at=datetime.now(UTC),
        )
        updated_messages = [*context, assistant_message]
        if created_bookmark is not None:
            updated_messages.append(
                DialogMessage(
                    id=f"system-{datetime.now(UTC).timestamp()}",
                    role="system",
                    content=(
                        "Новый собеседник добавлен. Прежний разговор сохранён — "
                        "можно вернуться в любой момент."
                    ),
                    created_at=datetime.now(UTC),
                )
            )
        store.save_actor_history(normalized_code, bookmark_id, updated_messages)

    # Сжатие давней истории — фоном, вне блокировки: живой диалог не ждёт LLM.
    asyncio.create_task(
        summarize_turn_after_save(normalized_code, bookmark_id, updated_messages)
    )

    return SendMessageResponse(
        code=normalized_code,
        reply=reply,
        messages=updated_messages,
        created_bookmark=created_bookmark,
    )


# --------------------------------------------------------------------------
# Раздача собранного фронтенда (artifacts/dialog/dist/public) тем же процессом.
# Маршрут регистрируется последним, чтобы не перехватывать /api/* и /docs.
# --------------------------------------------------------------------------


def _safe_static_path(relative_path: str) -> Path | None:
    """Возвращает файл внутри DIALOG_UI_DIST_DIR или None (защита от ../)."""
    dist_dir = DIALOG_UI_DIST_DIR.resolve()
    candidate = (dist_dir / relative_path).resolve()
    if candidate == dist_dir or dist_dir not in candidate.parents:
        return None
    if candidate.is_file():
        return candidate
    return None


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str) -> FileResponse:
    index_html = DIALOG_UI_DIST_DIR / "index.html"

    if not index_html.is_file():
        raise HTTPException(
            status_code=503,
            detail=(
                "Фронтенд не собран. Выполните: "
                "PORT=8011 BASE_PATH=/ pnpm --filter @workspace/dialog run build"
            ),
        )

    static_file = _safe_static_path(full_path)
    if static_file is not None:
        return FileResponse(static_file)

    # SPA-fallback: /dialog/smr001 и любые неизвестные пути отдают index.html,
    # роутинг (wouter) разберёт адрес на клиенте.
    return FileResponse(index_html)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
    )
