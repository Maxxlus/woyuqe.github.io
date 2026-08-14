# Woyuqe Mini App

Веб-интерфейс (Telegram Mini App) для бота Woyuqe: сетка иконок приложений,
плитка «Открыть сайт» (ИИ сам строит ссылку по названию) и полноценный чат
«Спросить ИИ». Это **статическая** страница (`index.html`) — весь код
на клиенте, обращается к API backend'а.

## Как это устроено

```
Telegram ──открывает──▶ Mini App (index.html, статика на GitHub Pages/Vercel)
                              │  fetch(API_BASE + /api/*)  +  initData в заголовке
                              ▼
                        backend (FastAPI, /api/*)  ← публичный HTTPS (туннель или VPS)
                              │  WebSocket
                              ▼
                        pc_agent → executor  (открывает приложения/сайты на ПК)
```

Аутентификация — та же, что в боте: Mini App присылает подписанный Telegram
`initData`, backend проверяет подпись ботом (HMAC) и сверяет `user.id`
с `AUTHORIZED_CHAT_IDS`. Подделать нельзя.

Иконки приложений берутся автоматически из `agent/executor.py` (`APP_MAP`),
дубли с одинаковым путём (`vpn`/`впн`/`v2raytun`, `yandex music`/`яндекс музыка`)
схлопываются в одну плитку. Добавишь приложение в `APP_MAP` — оно само
появится в интерфейсе.

## Настройка адреса backend

Три способа указать, куда Mini App шлёт запросы (в порядке приоритета):

1. `?api=https://твой-backend` в конце ссылки на приложение;
2. кнопка **⚙** в шапке приложения → вставить адрес (сохраняется в браузере);
3. поправить `DEFAULT_API_BASE` в начале `<script>` внутри `index.html`.

## Развёртывание фронтенда

### GitHub Pages
1. Положи `index.html` в корень репозитория `Maxxlus/maxxlus.github.io`
   (для user-сайта Pages раздаёт именно ветку `main`, корень).
2. Settings → Pages → Source: `main` / root.
3. Сайт будет на `https://maxxlus.github.io/`.

### Vercel
1. Импортируй репозиторий, framework preset: **Other** (статика).
2. Output/root — папка с `index.html`.

## Публичный backend (обязателен для Telegram)

Telegram открывает Mini App только по HTTPS, поэтому backend с ПК надо вывести
наружу. Самый быстрый способ — туннель:

```bash
# запускаешь backend
uvicorn backend.main:app --host 0.0.0.0 --port 8000
# в другом окне — туннель (даст https-адрес)
cloudflared tunnel --url http://localhost:8000
```

Полученный `https://...trycloudflare.com` — это твой `API_BASE` (вставь через ⚙)
и одновременно основа для `WEBAPP_URL` в `.env` (если раздаёшь фронт с backend —
адрес будет `https://...trycloudflare.com/app/`).

На VPS всё то же самое, только вместо туннеля — постоянный домен + reverse proxy
(nginx/caddy) с TLS.

## Подключение к боту

В `.env` укажи `WEBAPP_URL` (адрес развёрнутого интерфейса). При старте backend
сам поставит кнопку-меню чата, а команда `/start` (или `/app`) пришлёт кнопку
«🖥 Открыть Woyuqe».
