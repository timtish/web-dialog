import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  CloudSun,
  LifeBuoy,
  Send,
  Sparkles,
} from 'lucide-react';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import { Route, Switch, useLocation, Router as WouterRouter } from 'wouter';

type Role = 'assistant' | 'user';

type Message = {
  id: string;
  role: Role;
  text: string;
  time: string;
};

const queryClient = new QueryClient();

const initialMessages: Message[] = [
  {
    id: 'welcome',
    role: 'assistant',
    text: 'Добрый вечер, папа. Рад тебя видеть. Как прошёл день?',
    time: 'сегодня, 18:42',
  },
  {
    id: 'returning',
    role: 'user',
    text: 'В целом хорошо. Только что смотрел старые фотографии.',
    time: 'сегодня, 18:44',
  },
  {
    id: 'reply',
    role: 'assistant',
    text: 'Это хорошие вещи — иногда один снимок может вернуть целый день. Кто там был?',
    time: 'сегодня, 18:44',
  },
];

const starters = [
  'Расскажи что-нибудь интересное',
  'Хочу просто поговорить',
  'Помоги вспомнить название фильма',
];

function currentTime() {
  return new Intl.DateTimeFormat('ru-RU', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date());
}

function getMockReply(text: string) {
  const normalized = text.toLocaleLowerCase('ru-RU');

  if (normalized.includes('привет') || normalized.includes('здравств')) {
    return 'Привет. Хорошо, что ты заглянул. Я рядом и внимательно слушаю.';
  }
  if (normalized.includes('погод')) {
    return 'Сегодня за окном спокойно и свежо. Самое то для короткой прогулки или чашки чая у окна.';
  }
  if (normalized.includes('фильм') || normalized.includes('кино')) {
    return 'Давай попробуем вспомнить вместе. Что запомнилось сильнее всего — сюжет, актёр или музыка?';
  }
  if (normalized.includes('как дела') || normalized.includes('как ты')) {
    return 'У меня всё спокойно. А ты как сегодня — без подробностей, если не хочется.';
  }
  if (normalized.includes('спасибо')) {
    return 'Пожалуйста. Мне приятно быть рядом в такие обычные минуты.';
  }
  return 'Понимаю тебя. Давай не спешить — расскажи столько, сколько сейчас хочется.';
}

function Home() {
  const [messages, setMessages] = useState<Message[]>(initialMessages);
  const [draft, setDraft] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState(false);
  const [lastFailedText, setLastFailedText] = useState('');
  const replyTimer = useRef<number | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    return () => {
      if (replyTimer.current !== null) {
        window.clearTimeout(replyTimer.current);
      }
    };
  }, []);

  const submitMessage = (rawText: string, isRetry = false) => {
    const text = rawText.trim();
    if (!text || isSending) return;

    setError(false);
    if (!isRetry) {
      setMessages((current) => [
        ...current,
        { id: `user-${Date.now()}`, role: 'user', text, time: currentTime() },
      ]);
      setDraft('');
    }
    textareaRef.current?.focus();
    setIsSending(true);

    replyTimer.current = window.setTimeout(() => {
      const shouldShowError =
        !isRetry &&
        (text.toLocaleLowerCase('ru-RU').includes('ошибка') ||
          text.toLocaleLowerCase('ru-RU').includes('не отвечай'));

      if (shouldShowError) {
        setLastFailedText(text);
        setIsSending(false);
        setError(true);
        return;
      }

      setMessages((current) => [
        ...current,
        {
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          text: getMockReply(text),
          time: currentTime(),
        },
      ]);
      setIsSending(false);
    }, 720);
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    submitMessage(draft);
  };

  const handleDraftChange = (value: string) => {
    setDraft(value);
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = `${Math.min(textarea.scrollHeight, 130)}px`;
    }
  };

  return (
    <main className="papa-page">
      <div className="papa-shell">
        <header className="papa-topbar" data-testid="header-main">
          <a className="papa-brand" href="/" data-testid="link-home">
            <span className="brand-mark" aria-hidden="true">
              <span />
            </span>
            <span className="brand-wordmark">Папа-бот</span>
          </a>
          <div className="topbar-note" data-testid="status-companion">
            место для разговора
          </div>
        </header>

        <div className="papa-layout">
          <section className="conversation-column" aria-labelledby="welcome-heading">
            <p className="welcome-kicker">тихий вечер, без спешки</p>
            <h1 className="welcome-title" id="welcome-heading">
              Ну что, <em>поговорим?</em>
            </h1>
            <p className="welcome-subtitle">
              Можно спросить что угодно, вспомнить хорошее или просто оставить
              пару слов. Я здесь и никуда не тороплюсь.
            </p>

            <div className="conversation-log" aria-live="polite" data-testid="conversation-log">
              {messages.length === 0 && (
                <div className="empty-note" data-testid="empty-conversation">
                  Здесь пока тихо. Напиши первую мысль — разговор сам найдёт
                  дорогу.
                </div>
              )}
              {messages.map((message) => (
                <article className={`message-row ${message.role}`} key={message.id} data-testid={`message-${message.id}`}>
                  {message.role === 'assistant' && (
                    <div className="avatar bot" aria-hidden="true">
                      П
                    </div>
                  )}
                  <div className="message-bubble">
                    <div data-testid={`message-text-${message.id}`}>{message.text}</div>
                    <div className="message-meta">{message.time}</div>
                  </div>
                  {message.role === 'user' && (
                    <div className="avatar user" aria-hidden="true">
                      В
                    </div>
                  )}
                </article>
              ))}
              {isSending && (
                <div className="message-row" data-testid="status-sending">
                  <div className="avatar bot" aria-hidden="true">
                    П
                  </div>
                  <div className="message-bubble typing-bubble" aria-label="Папа-бот печатает">
                    <i />
                    <i />
                    <i />
                  </div>
                </div>
              )}
              {error && (
                <div className="error-card" role="alert" data-testid="status-error">
                  <LifeBuoy size={17} strokeWidth={1.8} aria-hidden="true" />
                  <span>Кажется, мысль не дошла. Ничего, можно попробовать ещё раз.</span>
                  <button
                    type="button"
                    onClick={() => submitMessage(lastFailedText, true)}
                    data-testid="button-retry"
                  >
                    Повторить
                  </button>
                </div>
              )}
            </div>

            <div className="composer-wrap">
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
                  placeholder="Напишите, что на душе..."
                  rows={1}
                  disabled={isSending}
                  data-testid="input-message"
                />
                <button
                  className="send-button"
                  type="submit"
                  aria-label="Отправить сообщение"
                  disabled={!draft.trim() || isSending}
                  data-testid="button-send"
                >
                  <Send size={19} strokeWidth={2.1} />
                </button>
              </form>
              <p className="composer-hint">Enter — отправить · Shift + Enter — новая строка</p>
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
              <div className="weather-temp" data-testid="text-temperature">+8°</div>
              <div className="weather-summary">пасмурно, но тепло</div>
              <div className="weather-place">Тула · вторник</div>
            </section>
            <div className="small-ritual" data-testid="text-daily-thought">
              <Sparkles size={15} color="#b46e55" strokeWidth={1.8} aria-hidden="true" />
              <p>«Хороший разговор — это тоже прогулка»</p>
              <span>маленькая мысль на сегодня</span>
            </div>
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