import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  CloudSun,
  LifeBuoy,
  Plus,
  Send,
  Sparkles,
} from 'lucide-react';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import { Route, Switch, useLocation, useRoute, Router as WouterRouter } from 'wouter';

type Role = 'assistant' | 'user' | 'system';

type Message = {
  id: string;
  role: Role;
  text: string;
  time: string;
};

const queryClient = new QueryClient();

type ApiMessage = {
  id: string;
  role: Role;
  content: string;
  created_at: string;
};

type Bookmark = {
  id: string;
  name: string;
  icon: string;
  prompt_file: string | null;
  created_at: string;
};

type Weather = {
  temperature: number;
  summary: string;
  place: string;
  updated_at: string;
};

type GreetingPayload = {
  text: string;
  time_of_day: 'утро' | 'день' | 'вечер' | 'ночь';
};

type HoroscopePayload = {
  horoscope: string;
};

function isDefaultBookmarkId(bookmarkId: string | undefined) {
  return !bookmarkId || bookmarkId === DEFAULT_BOOKMARK_ID;
}

function activeSpeakerName(bookmarks: Bookmark[], activeBookmark: string) {
  const active = bookmarks.find((bookmark) => bookmark.id === activeBookmark);
  if (!active || isDefaultBookmarkId(active.id)) {
    return DEFAULT_BOOKMARK_NAME;
  }
  return active.name;
}

type SendMessagePayload = {
  messages?: ApiMessage[];
  reply?: string;
  created_bookmark?: Bookmark | null;
  detail?: string;
};

type BookmarksPayload = {
  bookmarks: Bookmark[];
  active: string;
  thought?: string | null;
};

const starters = [
  'Расскажи что-нибудь интересное',
  'Хочу просто поговорить',
  'Хочу поговорить с поэтом Есениным',
  'Что сегодня нового?',
];

const DEFAULT_BOOKMARK_ID = 'default';
const DEFAULT_BOOKMARK_NAME = 'Просто спросить';
const DEFAULT_BOOKMARK_DESCRIPTION = 'Начать обычный разговор';
const COMPOSER_PLACEHOLDER = 'Напишите, что на душе...';
const CREATOR_PROMPT_ID = 'creator-prompt';
const CREATOR_PROMPT =
  'Опиши нового собеседника, им может быть поэт, политик или кто-то из повестей известных, например: «Сергей Есенин, поэт, говори поэтично про природу».';

function formatMessageTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  const today = new Date();
  const isToday =
    today.getFullYear() === date.getFullYear() &&
    today.getMonth() === date.getMonth() &&
    today.getDate() === date.getDate();
  const time = new Intl.DateTimeFormat('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);

  if (isToday) return `сегодня, ${time}`;
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function fromApiMessages(messages: ApiMessage[]): Message[] {
  return messages.map((message) => ({
    id: message.id,
    role: message.role,
    text: message.content,
    time: formatMessageTime(message.created_at),
  }));
}

function Home() {
  const [, routeParams] = useRoute('/dialog/:code');
  const code = (routeParams?.code ?? 'demo01').toLowerCase();
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState('');
  const [isLoadingHistory, setIsLoadingHistory] = useState(true);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState('');
  const [lastFailedText, setLastFailedText] = useState('');
  const [reloadToken, setReloadToken] = useState(0);
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([]);
  const [activeBookmark, setActiveBookmark] = useState('default');
  const [dailyThought, setDailyThought] = useState('«Хороший разговор — это тоже прогулка»');
  const [horoscope, setHoroscope] = useState('');
  const [weather, setWeather] = useState<Weather | null>(null);
  const [greeting, setGreeting] = useState('');
  const [isLoadingBookmarks, setIsLoadingBookmarks] = useState(true);
  const [isSwitchingBookmark, setIsSwitchingBookmark] = useState('');
  const [bookmarkError, setBookmarkError] = useState('');
  const [isRefreshingBookmarks, setIsRefreshingBookmarks] = useState(false);
  const [queueLength, setQueueLength] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const conversationLogRef = useRef<HTMLDivElement>(null);
  const composerWrapRef = useRef<HTMLDivElement>(null);
  const sendInFlightRef = useRef(false);
  const queueRef = useRef<{ text: string; addPending: boolean }[]>([]);

  // Курсор сразу живёт в поле ввода.
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  // Когда в диалоге появляется новое сообщение, поджимаем прокрутку так,
  // чтобы нижняя граница поля ввода совпала с нижней границей окна.
  useEffect(() => {
    const log = conversationLogRef.current;
    const composerWrap = composerWrapRef.current;
    if (!log || !composerWrap) return;

    // .composer-wrap «липнет» к низу экрана, поэтому его положение в потоке
    // восстанавливаем по концу лога плюс верхний отступ.
    const composerMarginTop = parseFloat(getComputedStyle(composerWrap).marginTop) || 0;
    const composerFlowBottom =
      log.getBoundingClientRect().bottom + composerMarginTop + composerWrap.offsetHeight;
    const scrollDelta = composerFlowBottom - window.innerHeight;
    if (scrollDelta <= 0) return;

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollBy({ top: scrollDelta, behavior: reduceMotion ? 'auto' : 'smooth' });
  }, [messages, error, isSending]);

  useEffect(() => {
    let cancelled = false;

    const loadDialog = async () => {
      setIsLoadingHistory(true);
      setError('');
      try {
        const response = await fetch(`/api/dialog/${encodeURIComponent(code)}`);
        const payload = (await response.json()) as
          | { messages?: ApiMessage[]; detail?: string }
          | undefined;
        if (!response.ok) {
          throw new Error(payload?.detail ?? 'Не удалось открыть разговор.');
        }
        if (!cancelled) {
          setMessages(fromApiMessages(payload?.messages ?? []));
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : 'Не удалось открыть разговор.',
          );
        }
      } finally {
        if (!cancelled) {
          setIsLoadingHistory(false);
        }
      }
    };

    void loadDialog();
    return () => {
      cancelled = true;
    };
  }, [code, reloadToken]);

  useEffect(() => {
    let cancelled = false;

    const loadBookmarks = async () => {
      setIsLoadingBookmarks(true);
      setBookmarkError('');
      try {
        const response = await fetch(`/api/bookmarks/${encodeURIComponent(code)}`);
        const payload = (await response.json()) as BookmarksPayload & { detail?: string };
        if (!response.ok) {
          throw new Error(payload.detail ?? 'Не удалось загрузить собеседников.');
        }
        if (!cancelled) {
          setBookmarks(payload.bookmarks ?? []);
          setActiveBookmark(payload.active ?? 'default');
          setDailyThought(payload.thought ?? '«Хороший разговор — это тоже прогулка»');
        }
      } catch (loadError) {
        if (!cancelled) {
          setBookmarkError(
            loadError instanceof Error
              ? loadError.message
              : 'Не удалось загрузить собеседников.',
          );
        }
      } finally {
        if (!cancelled) {
          setIsLoadingBookmarks(false);
        }
      }
    };

    void loadBookmarks();
    return () => {
      cancelled = true;
    };
  }, [code, reloadToken]);

  useEffect(() => {
    let cancelled = false;

    const loadWeather = async () => {
      try {
        const response = await fetch('/api/weather');
        const payload = (await response.json()) as Weather & { detail?: string };
        if (!response.ok) {
          return;
        }
        if (!cancelled) {
          setWeather({
            temperature: payload.temperature,
            summary: payload.summary,
            place: payload.place,
            updated_at: payload.updated_at,
          });
        }
      } catch {
        // Погода — второстепенная карточка: тихо остаёмся без данных.
      }
    };

    void loadWeather();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadGreeting = async () => {
      try {
        const response = await fetch('/api/greeting');
        const payload = (await response.json()) as GreetingPayload;
        if (!response.ok) {
          return;
        }
        if (!cancelled) {
          setGreeting(payload.text);
        }
      } catch {
        // Без приветствия показываем нейтральный fallback ниже.
      }
    };

    void loadGreeting();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadHoroscope = async () => {
      try {
        const response = await fetch('/api/horoscope');
        const payload = (await response.json()) as HoroscopePayload & { detail?: string };
        if (!response.ok) {
          return;
        }
        if (!cancelled) {
          setHoroscope(payload.horoscope);
        }
      } catch {
        // Гороскоп — второстепенная карточка: тихо остаёмся на фолбэке.
      }
    };

    void loadHoroscope();
    return () => {
      cancelled = true;
    };
  }, []);

  // Обрабатывает очередь сообщений по одной: пока модель отвечает, следующие
  // сообщения спокойно ждут своей очереди и отправляются автоматически.
  const processQueue = async () => {
    if (sendInFlightRef.current) return;
    const next = queueRef.current.shift();
    if (!next) return;

    sendInFlightRef.current = true;
    setError('');
    setIsSending(true);
    setQueueLength(queueRef.current.length);
    if (next.addPending) {
      setMessages((current) => [
        ...current,
        {
          id: `pending-${Date.now()}`,
          role: 'user',
          text: next.text,
          time: 'сейчас',
        },
      ]);
    }
    textareaRef.current?.focus();

    try {
      const response = await fetch(
        `/api/dialog/${encodeURIComponent(code)}/messages`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: next.text }),
        },
      );
      const payload = (await response.json()) as SendMessagePayload;
      if (!response.ok) {
        throw new Error(payload?.detail ?? 'Собеседник пока не отвечает.');
      }
      setMessages(fromApiMessages(payload?.messages ?? []));
      if (payload?.created_bookmark) {
        // Новый собеседник уже в ответе API — показываем сразу, не дожидаясь
        // медленного /api/bookmarks (тот ждёт LLM-«мысль» и запаздывает).
        const created = payload.created_bookmark;
        setBookmarks((current) =>
          current.some((bookmark) => bookmark.id === created.id)
            ? current
            : [...current, created],
        );
        // И параллельно обновляем список из API, когда он подтянется.
        void refreshBookmarks();
      }
    } catch (sendError) {
      setLastFailedText(next.text);
      setError(
        sendError instanceof Error
          ? sendError.message
          : 'Собеседник пока не отвечает.',
      );
    } finally {
      sendInFlightRef.current = false;
      setIsSending(false);
      void processQueue();
    }
  };

  // Ставит сообщение в очередь; processQueue запускает (или продолжает) отправку.
  const submitMessage = (rawText: string, isRetry = false) => {
    const text = rawText.trim();
    if (!text) return;
    queueRef.current.push({ text, addPending: !isRetry });
    if (!isRetry) {
      setDraft('');
    }
    void processQueue();
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submitMessage(draft);
  };

  const refreshBookmarks = async () => {
    if (isRefreshingBookmarks) return;
    setIsRefreshingBookmarks(true);
    setBookmarkError('');
    try {
      const response = await fetch(`/api/bookmarks/${encodeURIComponent(code)}`);
      const payload = (await response.json()) as BookmarksPayload & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail ?? 'Не удалось загрузить собеседников.');
      }
      setBookmarks(payload.bookmarks ?? []);
      setActiveBookmark(payload.active ?? 'default');
      setDailyThought(payload.thought ?? '«Хороший разговор — это тоже прогулка»');
    } catch (refreshError) {
      setBookmarkError(
        refreshError instanceof Error
          ? refreshError.message
          : 'Не удалось загрузить собеседников.',
      );
    } finally {
      setIsRefreshingBookmarks(false);
    }
  };

  const handleActivateBookmark = async (
    bookmarkId: string,
    options: { allowSame?: boolean } = {},
  ) => {
    if (isSwitchingBookmark) return;
    if (bookmarkId === activeBookmark && !options.allowSame) return;

    setBookmarkError('');
    setIsSwitchingBookmark(bookmarkId);
    try {
      const response = await fetch(
        `/api/bookmarks/${encodeURIComponent(code)}/${encodeURIComponent(bookmarkId)}`,
        { method: 'PUT' },
      );
      const payload = (await response.json()) as BookmarksPayload & {
        messages?: ApiMessage[];
        detail?: string;
      };
      if (!response.ok) {
        throw new Error(payload.detail ?? 'Не удалось сменить собеседника.');
      }
      setBookmarks(payload.bookmarks ?? []);
      setActiveBookmark(payload.active ?? bookmarkId);
      setDailyThought(payload.thought ?? '«Хороший разговор — это тоже прогулка»');
      setMessages(fromApiMessages(payload.messages ?? []));
    } catch (activateError) {
      setBookmarkError(
        activateError instanceof Error
          ? activateError.message
          : 'Не удалось сменить собеседника.',
      );
    } finally {
      setIsSwitchingBookmark('');
    }
  };

  const handleOpenCreatorMode = () => {
    setBookmarkError('');
    // Включаем серверную сессию создания (15 минут): в это время общий диалог
    // ведёт агент-создатель, который болтает, уточняет и создаёт собеседников.
    const startCreator = async () => {
      const response = await fetch(
        `/api/bookmarks/${encodeURIComponent(code)}/creator`,
        { method: 'POST' },
      );
      const payload = (await response.json()) as BookmarksPayload & { detail?: string };
      if (!response.ok) {
        throw new Error(payload.detail ?? 'Не удалось включить создание собеседника.');
      }
      setBookmarks(payload.bookmarks ?? []);
    };
    // Создание собеседника живёт в общем диалоге: переключаемся на встроенного
    // собеседника «Просто спросить» — история при этом сохраняется.
    // Подсказку добавляем после переключения: ответ PUT несёт историю диалога
    // и затёр бы сообщение, добавленное раньше него.
    void (async () => {
      try {
        await startCreator();
      } catch (creatorError) {
        setBookmarkError(
          creatorError instanceof Error
            ? creatorError.message
            : 'Не удалось включить создание собеседника.',
        );
        return;
      }
      await handleActivateBookmark(DEFAULT_BOOKMARK_ID, { allowSame: true });
      setMessages((current) =>
        current.some((message) => message.id === CREATOR_PROMPT_ID)
          ? current
          : [
              ...current,
              {
                id: CREATOR_PROMPT_ID,
                role: 'assistant',
                text: CREATOR_PROMPT,
                time: 'сейчас',
              },
            ],
      );
    })();
    textareaRef.current?.focus();
  };

  const handleDraftChange = (value: string) => {
    setDraft(value);
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = `${Math.min(textarea.scrollHeight, 130)}px`;
    }
  };

  const speakerName = activeSpeakerName(bookmarks, activeBookmark);
  const avatarLetter = speakerName.trim().charAt(0).toUpperCase() || 'ИИ';
  const composerPlaceholder = isDefaultBookmarkId(activeBookmark)
    ? COMPOSER_PLACEHOLDER
    : `Напишите ${speakerName}у...`;
  const emptyGreeting = greeting || 'Добрый день.';
  const emptyHint = isDefaultBookmarkId(activeBookmark)
    ? 'Можно спросить что угодно или попросить создать нового собеседника'
    : `Я — ${speakerName}. Рад встрече. О чём поговорим?`;

  const formatWeatherTemperature = (value: number) => {
    const rounded = Math.round(value);
    return `${rounded > 0 ? '+' : ''}${rounded}°`;
  };

  const formatWeatherPlace = (value: Weather) => {
    const weekday = new Intl.DateTimeFormat('ru-RU', { weekday: 'long' }).format(
      new Date(value.updated_at),
    );
    return `${value.place} · ${weekday}`;
  };

  return (
    <main className="papa-page">
      <div className="papa-shell">

        <div className="papa-layout">
          <section className="conversation-column" aria-labelledby="welcome-heading">
            <h1 className="welcome-title" id="welcome-heading">
              Ну что, <em>поговорим?</em>
            </h1>

            <div
              className="conversation-log"
              aria-live="polite"
              data-testid="conversation-log"
              ref={conversationLogRef}
            >
              {isLoadingHistory && (
                <div className="empty-note" data-testid="loading-conversation">
                  Загружаю наш разговор...
                </div>
              )}
              {!isLoadingHistory && messages.length === 0 && (
                <div className="empty-note" data-testid="empty-conversation">
                  <strong>{emptyGreeting}</strong>
                  <span>{emptyHint}</span>
                </div>
              )}
              {messages.map((message) => (
                <article className={`message-row ${message.role}`} key={message.id} data-testid={`message-${message.id}`}>
                  {message.role === 'assistant' && (
                    <div className="message-side">
                      <div className="avatar bot" aria-hidden="true">
                        {avatarLetter}
                      </div>
                      <div className="message-meta">{message.time}</div>
                    </div>
                  )}
                  {message.role === 'system' && (
                    <div className="system-mark" aria-hidden="true">
                      <Sparkles size={14} strokeWidth={1.8} />
                    </div>
                  )}
                  <div className="message-bubble">
                    <div data-testid={`message-text-${message.id}`}>{message.text}</div>
                  </div>
                  {message.role === 'user' && (
                    <div className="message-side">
                      <div className="avatar user" aria-hidden="true">
                        В
                      </div>
                      <div className="message-meta">{message.time}</div>
                    </div>
                  )}
                </article>
              ))}
              {isSending && (
                <div className="message-row" data-testid="status-sending">
                  <div className="message-side">
                    <div className="avatar bot" aria-hidden="true">
                      {avatarLetter}
                    </div>
                  </div>
                  <div className="message-bubble typing-bubble" aria-label="бот печатает">
                    <i />
                    <i />
                    <i />
                    {queueLength > 0 && (
                      <span className="typing-queue" data-testid="queue-count">
                        +{queueLength} в очереди
                      </span>
                    )}
                  </div>
                </div>
              )}
              {error && (
                <div className="error-card" role="alert" data-testid="status-error">
                  <LifeBuoy size={17} strokeWidth={1.8} aria-hidden="true" />
                  <span>{error}</span>
                  <button
                    type="button"
                    onClick={() => {
                      if (lastFailedText) {
                        void submitMessage(lastFailedText, true);
                      } else {
                        setReloadToken((value) => value + 1);
                      }
                    }}
                    data-testid="button-retry"
                  >
                    Повторить
                  </button>
                </div>
              )}
            </div>

            <div className="composer-wrap" ref={composerWrapRef}>
              <form className="composer" onSubmit={handleSubmit} data-testid="form-message">
                <label className="sr-only" htmlFor="message-input">
                  Напишите сообщение
                </label>
                <textarea
                  id="message-input"
                  ref={textareaRef}
                  value={draft}
                  onChange={(event) => handleDraftChange(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault();
                      submitMessage(draft);
                    }
                  }}
                  placeholder={composerPlaceholder}
                  rows={1}
                  data-testid="input-message"
                />
                <button
                  className="send-button"
                  type="submit"
                  aria-label="Отправить сообщение"
                  disabled={!draft.trim()}
                  data-testid="button-send"
                >
                  <Send size={19} strokeWidth={2.1} />
                </button>
              </form>
              <p className="composer-hint">
                Enter — отправить · Shift + Enter — новая строка
                {isSending && queueLength > 0 ? ` · В очереди: ${queueLength}` : ''}
              </p>
            </div>

            <section className="starter-section" aria-labelledby="starter-heading">
              <h2 className="section-label" id="starter-heading">
                Если не знаешь, с чего начать
              </h2>
              <div className="starter-list">
                {starters.map((starter, index) => (
                  <button
                    className="starter-chip"
                    type="button"
                    key={starter}
                    onClick={() => {
                      handleDraftChange(starter);
                      textareaRef.current?.focus();
                    }}
                    data-testid={`button-starter-${index}`}
                  >
                    {starter}
                  </button>
                ))}
              </div>
            </section>
          </section>

          <aside className="side-column" aria-label="Полезное на сегодня">
            <section className="weather-card" data-testid="card-weather">
              <div className="weather-heading">
                <span>Сегодня</span>
                <CloudSun className="weather-icon" size={24} strokeWidth={1.7} aria-hidden="true" />
              </div>
              {weather ? (
                <>
                  <div className="weather-temp" data-testid="text-temperature">
                    {formatWeatherTemperature(weather.temperature)}
                  </div>
                  <div className="weather-summary">{weather.summary}</div>
                  <div className="weather-place">{formatWeatherPlace(weather)}</div>
                </>
              ) : (
                <>
                  <div className="weather-temp" data-testid="text-temperature">—°</div>
                  <div className="weather-summary">Погода пока не загрузилась</div>
                </>
              )}
            </section>
            <div className="small-ritual" data-testid="text-daily-thought">
              <Sparkles size={15} color="#b46e55" strokeWidth={1.8} aria-hidden="true" />
              <p>{horoscope && isDefaultBookmarkId(activeBookmark) ? horoscope : dailyThought}</p>
              <span>{DEFAULT_BOOKMARK_DESCRIPTION}</span>
            </div>
            <section className="bookmarks-card" aria-labelledby="bookmarks-heading">
              <div className="bookmarks-heading">
                <div>
                  <p className="side-label" id="bookmarks-heading">Собеседники</p>
                  <span className="bookmarks-count">Мои собеседники</span>
                </div>
                <Sparkles size={16} strokeWidth={1.7} aria-hidden="true" />
              </div>
              <div className="bookmarks-list">
                {isLoadingBookmarks && (
                  <div className="bookmarks-loading">Загружаю список...</div>
                )}
                {!isLoadingBookmarks && bookmarks.map((bookmark) => (
                  <button
                    className={`bookmark-item ${bookmark.id === activeBookmark ? 'active' : ''}`}
                    type="button"
                    key={bookmark.id}
                    onClick={() => void handleActivateBookmark(bookmark.id)}
                    disabled={Boolean(isSwitchingBookmark)}
                    aria-pressed={bookmark.id === activeBookmark}
                  >
                    <span className="bookmark-icon" aria-hidden="true">{bookmark.icon}</span>
                    <span className="bookmark-name">{bookmark.name}</span>
                    {isSwitchingBookmark === bookmark.id && <span className="bookmark-spinner" aria-hidden="true" />}
                  </button>
                ))}
              </div>
              {bookmarkError && <p className="bookmark-error" role="alert">{bookmarkError}</p>}
              <button
                className="add-bookmark-button"
                type="button"
                onClick={handleOpenCreatorMode}
                disabled={isLoadingBookmarks}
              >
                <Plus size={16} strokeWidth={2} aria-hidden="true" />
                Добавить нового
              </button>
            </section>
          </aside>
        </div>
      </div>
    </main>
  );
}

function Router() {
  return (
    <RoutedErrorBoundary>
      <Switch>
        <Route path="/dialog/:code" component={Home} />
        <Route path="/" component={Home} />
        <Route component={NotFound} />
      </Switch>
    </RoutedErrorBoundary>
  );
}

function RoutedErrorBoundary({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  return <ErrorBoundary resetKey={location}>{children}</ErrorBoundary>;
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
          <Router />
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
