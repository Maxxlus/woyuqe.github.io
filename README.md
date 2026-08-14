# LifeOS v2

Автономный AI-агент для управления компьютером через Telegram.

## Архитектура

```
Telegram
   ↓  (сообщение пользователя)
Backend  ←→  Ollama (локальная LLM)
   ↓  (JSON план через WebSocket)
PC Agent  (Windows)
   ↓
Executor → выполняет actions на ПК
   ↓
Результаты / скриншоты → Backend → Telegram
```

**Ключевые принципы:**
- Агент знает только **Action Protocol** (JSON). Никакого русского языка.
- Чтобы сменить LLM (Ollama → GPT) — меняется **только** `llm_router.py`
- Чтобы добавить новое действие — добавляется action в `protocol/actions.py` + handler в `executor.py`

---

## Быстрый старт

### 1. Установка зависимостей

```bash
pip install -r requirements.txt
```

### 2. Конфиг

```bash
cp .env.example .env
# Заполнить BOT_TOKEN в .env
```

### 3. Установка Ollama

```bash
# Скачать с https://ollama.ai
# Затем скачать модель:
ollama pull qwen2.5vl:7b
```

**Почему qwen2.5vl:7b:**
- Понимает русский язык
- Поддерживает vision (анализ скриншотов)
- ~5GB — умещается на большинстве GPU/CPU
- Лучший баланс скорость/качество для локального запуска

**Альтернативы если мало RAM:**
- `moondream:latest` (~1.7GB) — легковесный, есть vision
- `qwen2.5:3b` (~2GB) — только текст, быстрый

### 4. Запуск Backend

```bash
# На любой машине (можно тот же ПК)
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### 5. Запуск агента на Windows

```bash
python agent/pc_agent.py
```

---

## Структура проекта

```
lifeos_v2/
├── .env.example
├── requirements.txt
│
├── backend/
│   ├── main.py          ← FastAPI: Telegram polling + WebSocket сервер
│   └── llm_router.py    ← Ollama / OpenAI — ЕДИНСТВЕННОЕ место для смены LLM
│
└── agent/
    ├── pc_agent.py      ← Запускается на Windows, получает планы, выполняет
    ├── executor.py      ← Выполняет Action объекты (pyautogui, pycaw, ...)
    │
    └── protocol/
        └── actions.py   ← Action Protocol: схемы + системный промпт для LLM

webapp/
└── index.html          ← Telegram Mini App (веб-интерфейс). См. webapp/README.md
```

---

## Mini App (веб-интерфейс)

`webapp/index.html` — Telegram Mini App: сетка иконок приложений (из `APP_MAP`),
плитка «Открыть сайт» (ИИ строит ссылку по названию) и чат «Спросить ИИ».
Статическая страница, ходит в новые эндпоинты backend'а `/api/*`; авторизация —
по тому же Telegram `chat_id` (проверка подписи `initData`). Подробности,
развёртывание и настройка — в `webapp/README.md`.

Новые переменные в `.env`: `WEBAPP_URL` (адрес интерфейса для кнопки в боте),
`WEBAPP_DEV_CHAT_ID` (только для локальной отладки). См. `.env.example`.

---

## Action Protocol

Полный список поддерживаемых действий:

| action | параметры | описание |
|--------|-----------|----------|
| `open_app` | `app` | Открыть приложение |
| `close_app` | `app` | Закрыть приложение |
| `click` | `x, y` | Клик мышью |
| `double_click` | `x, y` | Двойной клик |
| `right_click` | `x, y` | Правый клик |
| `move_mouse` | `x, y` | Переместить мышь |
| `scroll` | `direction, amount` | Скролл |
| `press` | `key` | Нажать клавишу |
| `hotkey` | `keys: []` | Комбинация клавиш |
| `type` | `text` | Напечатать текст |
| `wait` | `seconds` | Пауза |
| `screenshot` | `send_to_chat` | Скриншот → Telegram |
| `find_text` | `text` | Найти текст на экране (OCR) |
| `find_image` | `image, click_on_found` | Найти изображение |
| `set_volume` | `percent` | Громкость 0-100% |
| `say` | `text` | Написать пользователю в Telegram |
| `open_url` | `url` | Открыть URL в браузере |
| `get_clipboard` | — | Прочитать буфер обмена |
| `set_clipboard` | `text` | Записать в буфер |

---

## Смена LLM

Всё в `.env`:

```env
# Ollama (бесплатно, локально)
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5vl:7b

# GPT-4o-mini (платно, быстро)
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# Groq (бесплатный tier, быстро)
LLM_PROVIDER=openai
OPENAI_URL=https://api.groq.com/openai/v1
OPENAI_API_KEY=gsk_...
OPENAI_MODEL=llama-3.1-70b-versatile
```

Агент (`pc_agent.py`) при этом **не меняется вообще**.

---

## Debug endpoints

```
GET  /health        — статус backend + LLM + подключённые агенты
GET  /agents        — список подключённых агентов
POST /plan          — тест: текст → план (без выполнения)
```

Пример:
```bash
curl -X POST http://localhost:8000/plan \
  -H "Content-Type: application/json" \
  -d '{"text": "открой телеграм и сделай скриншот"}'
```

Ответ:
```json
{
  "input": "открой телеграм и сделай скриншот",
  "validated": [
    {"action": "open_app", "app": "Telegram"},
    {"action": "wait", "seconds": 2},
    {"action": "screenshot", "send_to_chat": true},
    {"action": "say", "text": "Открыл Telegram и сделал скриншот"}
  ]
}
```

---

## Добавить новое действие

1. В `protocol/actions.py` — добавить Pydantic модель и зарегистрировать в `ACTION_REGISTRY`
2. В `executor.py` — добавить метод `_do_<action_name>()`
3. В `SYSTEM_PROMPT` в `actions.py` — добавить строку с описанием для LLM

Агент и backend менять не нужно.
