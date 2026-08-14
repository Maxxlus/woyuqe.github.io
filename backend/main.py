"""
backend/main.py — Woyuqe v3.2

ИЗМЕНЕНИЕ: plan_with_history теперь возвращает dict {"reply": str, "actions": list}.
handle_update адаптирован под новую структуру:
  - reply отправляется в Telegram ВСЕГДА (даже если actions пустой — это обычный диалог)
  - actions выполняются на агенте, если список не пустой
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.llm_router import plan_with_history, check_available, resolve_url
from backend import reminders
from backend.webapp_api import create_webapp_router
from agent.protocol.actions import build_system_prompt, validate_plan

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# URL развёрнутого Mini App (GitHub Pages / Vercel / VPS). HTTPS обязателен.
# Используется для кнопки запуска в боте и кнопки-меню чата.
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

# Только для локальной отладки Mini App в обычном браузере (без Telegram).
# В проде оставить пустым!
_dev = os.getenv("WEBAPP_DEV_CHAT_ID", "").strip()
WEBAPP_DEV_CHAT_ID: int | None = int(_dev) if _dev.isdigit() else None

# Путь к executor.py — из него берётся список приложений (APP_MAP).
EXECUTOR_PATH = os.path.join(os.path.dirname(__file__), "..", "agent", "executor.py")

# Как часто проверять, не пора ли отправить напоминание (секунды).
REMINDER_CHECK_INTERVAL = int(os.getenv("REMINDER_CHECK_INTERVAL", "60"))


AUTHORIZED_CHAT_IDS: set[int] = {
    int(cid.strip())
    for cid in os.getenv("AUTHORIZED_CHAT_IDS", "1616991465").split(",")
    if cid.strip().isdigit()
}


def is_authorized(chat_id: int) -> bool:
    return chat_id in AUTHORIZED_CHAT_IDS


_tg_client: httpx.AsyncClient | None = None

MAX_HISTORY = 20
conversation_history: dict[int, list[dict]] = defaultdict(list)


def add_to_history(chat_id: int, role: str, content: str):
    h = conversation_history[chat_id]
    h.append({"role": role, "content": content})
    if len(h) > MAX_HISTORY:
        conversation_history[chat_id] = h[-MAX_HISTORY:]


def get_history(chat_id: int) -> list[dict]:
    return conversation_history[chat_id].copy()


class AgentManager:
    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._response_queues: dict[str, asyncio.Queue] = {}

    async def connect(self, agent_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[agent_id] = ws
        self._response_queues[agent_id] = asyncio.Queue()
        print(f"[WS] ✓ Agent connected: {agent_id}")

    def disconnect(self, agent_id: str):
        self._connections.pop(agent_id, None)
        self._response_queues.pop(agent_id, None)
        print(f"[WS] Agent disconnected: {agent_id}")

    async def send_plan(self, agent_id: str, actions: list[dict], request_id: str):
        ws = self._connections.get(agent_id)
        if ws is None:
            raise RuntimeError(f"Agent '{agent_id}' not connected")
        await ws.send_json(
            {"type": "plan", "request_id": request_id, "actions": actions}
        )

    async def wait_result(
        self, agent_id: str, request_id: str, timeout: float = 60
    ) -> dict:
        queue = self._response_queues.get(agent_id)
        if queue is None:
            raise RuntimeError(f"No queue for agent '{agent_id}'")
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": "Agent timeout (60s)"}

    async def put_result(self, agent_id: str, result: dict):
        q = self._response_queues.get(agent_id)
        if q:
            await q.put(result)

    def get_default(self) -> str | None:
        ids = list(self._connections.keys())
        return ids[0] if ids else None


agent_manager = AgentManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tg_client
    _tg_client = httpx.AsyncClient(timeout=10.0)
    reminders.init_db()
    polling_task = asyncio.create_task(telegram_polling())
    reminder_task = asyncio.create_task(reminder_checker_loop())
    await _tg_set_menu_button()
    print(f"[BACKEND] ✓ Started")
    print(f"[BACKEND] BOT_TOKEN : {'SET' if BOT_TOKEN else 'EMPTY!'}")
    print(f"[BACKEND] Authorized: {AUTHORIZED_CHAT_IDS}")
    print(f"[BACKEND] Reminders DB: {reminders.DB_PATH}")
    print(f"[BACKEND] Mini App  : {WEBAPP_URL or '(WEBAPP_URL не задан)'}")
    if WEBAPP_DEV_CHAT_ID is not None:
        print(f"[BACKEND] ⚠ DEV MODE: API открыт без Telegram для chat_id={WEBAPP_DEV_CHAT_ID}")
    yield
    polling_task.cancel()
    reminder_task.cancel()
    if _tg_client:
        await _tg_client.aclose()
    print(f"[BACKEND] ✗ Stopped")


app = FastAPI(title="LifeOS Backend", version="3.2", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.websocket("/ws/agent/{agent_id}")
async def agent_websocket(ws: WebSocket, agent_id: str):
    await agent_manager.connect(agent_id, ws)
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "result":
                await agent_manager.put_result(agent_id, data)

            elif msg_type == "screenshot":
                chat_id = data.get("chat_id")
                img_b64 = data.get("image_b64")
                caption = data.get("caption", "📸 Скриншот")
                if chat_id and img_b64 and is_authorized(int(chat_id)):
                    asyncio.create_task(
                        _tg_send_photo(chat_id, base64.b64decode(img_b64), caption)
                    )

            elif msg_type == "say":
                chat_id = data.get("chat_id")
                text = data.get("text", "")
                if chat_id and text and is_authorized(int(chat_id)):
                    asyncio.create_task(_tg_send_message(chat_id, text))

            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        agent_manager.disconnect(agent_id)


last_update_id = 0


_STAGE_LABEL = {
    "1h": "⏰ Через час",
    "15m": "⏰ Через 15 минут",
    "due": "🔔 Сейчас",
}


async def reminder_checker_loop():
    """
    Раз в REMINDER_CHECK_INTERVAL секунд проверяет SQLite на предмет
    заметок, для которых пора отправить напоминание, и шлёт их в Telegram.
    Полностью независима от PC-агента и WebSocket-соединений.
    """
    print(f"[REMINDERS] Starting loop (interval={REMINDER_CHECK_INTERVAL}s)...")
    while True:
        try:
            due = reminders.get_due_notifications()
            for reminder, stage in due:
                label = _STAGE_LABEL.get(stage, "🔔")
                text = f"{label}: {reminder.text}"
                ok = await _tg_send_message(reminder.chat_id, text)
                if ok:
                    reminders.mark_notified(reminder.id, stage)
                else:
                    print(
                        f"[REMINDERS] ✗ Не удалось отправить chat_id={reminder.chat_id}, "
                        f"id={reminder.id}, stage={stage} — попробуем снова на следующем цикле"
                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            import traceback

            print(f"[REMINDERS] Error: {type(e).__name__}: {e}")
            print(traceback.format_exc())
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)


def _format_reminders_list(items: list[reminders.Reminder]) -> str:
    if not items:
        return "🗒 У вас пока нет активных заметок."
    lines = ["🗒 <b>Ваши заметки:</b>"]
    for r in items:
        dt_str = r.due_at.strftime("%d.%m %H:%M")
        lines.append(f"#{r.id} — {dt_str} — {r.text}")
    return "\n".join(lines)


async def telegram_polling():
    global last_update_id
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
    ) as client:
        print("[POLLING] Starting...")
        while True:
            try:
                r = await client.get(
                    f"{API_BASE}/getUpdates",
                    params={"offset": last_update_id + 1, "timeout": 20},
                )
                updates = r.json().get("result", [])
                if updates:
                    print(f"[POLLING] {len(updates)} update(s)")
                for update in updates:
                    last_update_id = update["update_id"]
                    asyncio.create_task(handle_update(update))
            except httpx.ReadTimeout:
                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                import traceback

                print(f"[POLLING] Error: {type(e).__name__}: {e}")
                print(traceback.format_exc())
                await asyncio.sleep(3)


async def handle_update(update: dict):
    message = update.get("message", {})
    if not message or "text" not in message:
        return

    chat_id = int(message["chat"]["id"])
    user_text = message["text"].strip()

    if not is_authorized(chat_id):
        print(f"[AUTH] ⛔ Unauthorized access: chat_id={chat_id}, text='{user_text}'")
        await _tg_send_message(chat_id, "⛔ Доступ запрещён.")
        return

    print(f"\n[UPDATE] chat_id={chat_id}, text='{user_text}'")

    if user_text.lower() in ["/start", "/app", "/menu", "/приложение"]:
        await _send_webapp_launcher(chat_id)
        return

    if user_text.lower() in ["/clear", "/забудь", "забудь всё", "очисти память"]:
        conversation_history[chat_id] = []
        await _tg_send_message(chat_id, "🧹 Память очищена.")
        return

    if user_text.lower() in ["/memory", "/память"]:
        history = get_history(chat_id)
        if not history:
            await _tg_send_message(chat_id, "🧠 Память пуста.")
        else:
            lines = [f"🧠 <b>Память ({len(history)} сообщений):</b>"]
            for msg in history[-10:]:
                role = "👤" if msg["role"] == "user" else "🤖"
                lines.append(f"{role} {msg['content'][:80]}")
            await _tg_send_message(chat_id, "\n".join(lines))
        return

    if user_text.lower() in ["/notes", "/заметки"]:
        items = reminders.list_upcoming(chat_id)
        await _tg_send_message(chat_id, _format_reminders_list(items))
        return

    add_to_history(chat_id, "user", user_text)
    thinking_id = await _tg_send_message_get_id(chat_id, "🧠 Думаю...")

    print(f"[UPDATE] → LLM: '{user_text}' (history: {len(get_history(chat_id))} msgs)")
    try:
        llm_result = await plan_with_history(
            user_text, build_system_prompt(), get_history(chat_id)
        )
    except Exception as e:
        import traceback

        print(f"[UPDATE] LLM error: {traceback.format_exc()}")
        await _tg_delete_message(chat_id, thinking_id)
        await _tg_send_message(chat_id, f"❌ Ошибка LLM: {e}")
        return

    reply = llm_result.get("reply", "")
    raw_actions = llm_result.get("actions", [])

    print(f"[UPDATE] REPLY: {reply}")
    print(f"[UPDATE] RAW ACTIONS: {raw_actions}")

    # Сохраняем ответ в историю
    if reply:
        add_to_history(chat_id, "assistant", reply)

    # ── Убираем "Думаю..." и показываем ответ ──────────────────────────────
    await _tg_delete_message(chat_id, thinking_id)
    if reply:
        await _tg_send_message(chat_id, f"🤖 {reply}")

    # ── Если действий нет — это обычный разговор, на этом всё ─────────────
    if not raw_actions:
        print(f"[UPDATE] ✓ Диалог без действий")
        return

    # ── Выполняем действия (заметки — локально, остальное — на ПК-агенте) ──
    await dispatch_actions(chat_id, raw_actions, notify_telegram=True)


async def _handle_reminder_action(chat_id: int, raw: dict) -> None:
    """
    Выполняет add_reminder / list_reminders / cancel_reminder локально
    на backend (без участия PC-агента) и отправляет результат в Telegram.
    Ответ от модели (reply) уже был отправлен в handle_update — здесь
    только конкретный результат самого действия (например, список заметок).
    """
    action = raw.get("action")

    if action == "add_reminder":
        text = raw.get("text", "").strip()
        due_at_raw = raw.get("due_at", "")
        if not text or not due_at_raw:
            await _tg_send_message(
                chat_id, "⚠️ Не удалось разобрать заметку (нет текста или даты)."
            )
            return
        try:
            due_at = datetime.fromisoformat(due_at_raw)
        except ValueError:
            print(f"[REMINDERS] ✗ Некорректный due_at от LLM: '{due_at_raw}'")
            await _tg_send_message(
                chat_id, "⚠️ Не удалось разобрать дату/время заметки."
            )
            return
        reminders.add_reminder(chat_id, text, due_at)
        print(
            f"[REMINDERS] ✓ Добавлена заметка chat_id={chat_id}, due_at={due_at}, text='{text}'"
        )
        # Подтверждение уже было в reply от LLM — доп. сообщение не шлём,
        # чтобы не дублировать. Если хотите дублировать — раскомментируйте:
        # await _tg_send_message(chat_id, f"✅ Заметка поставлена на {due_at.strftime('%d.%m %H:%M')}: {text}")

    elif action == "list_reminders":
        items = reminders.list_upcoming(chat_id)
        await _tg_send_message(chat_id, _format_reminders_list(items))

    elif action == "cancel_reminder":
        reminder_id = raw.get("reminder_id")
        if reminder_id is None:
            await _tg_send_message(chat_id, "⚠️ Не указан id заметки для отмены.")
            return
        ok = reminders.cancel_reminder(int(reminder_id), chat_id)
        if ok:
            await _tg_send_message(chat_id, f"🗑 Заметка #{reminder_id} отменена.")
        else:
            await _tg_send_message(chat_id, f"⚠️ Заметка #{reminder_id} не найдена.")


async def dispatch_actions(
    chat_id: int, raw_actions: list[dict], notify_telegram: bool = False
) -> dict:
    """
    Единая точка выполнения плана LLM. Заметки/напоминания выполняются
    локально (backend), остальные действия отправляются PC-агенту.

    notify_telegram=True  — шлёт ошибки/статусы в Telegram (режим бота).
    notify_telegram=False — молча возвращает результат (режим Mini App).

    Возвращает {"ran": bool, "ok": bool, "error": str, "did_reminder": bool}.
    """
    if not raw_actions:
        return {"ran": False, "ok": True, "error": "", "did_reminder": False}

    REMINDER_ACTIONS = {"add_reminder", "list_reminders", "cancel_reminder"}
    local_actions = [a for a in raw_actions if a.get("action") in REMINDER_ACTIONS]
    agent_actions = [a for a in raw_actions if a.get("action") not in REMINDER_ACTIONS]

    for raw in local_actions:
        await _handle_reminder_action(chat_id, raw)
    did_reminder = bool(local_actions)

    if not agent_actions:
        return {"ran": False, "ok": True, "error": "", "did_reminder": did_reminder}

    agent_id = agent_manager.get_default()
    if agent_id is None:
        if notify_telegram:
            await _tg_send_message(chat_id, "❌ Агент не подключён. Запустите pc_agent.py.")
        return {"ran": False, "ok": False, "error": "Агент не подключён", "did_reminder": did_reminder}

    validated = validate_plan(agent_actions)
    print(f"[DISPATCH] VALIDATED: {[v.action for v in validated]}")
    if not validated:
        if notify_telegram:
            await _tg_send_message(chat_id, "⚠️ Не удалось разобрать действия.")
        return {"ran": False, "ok": False, "error": "Не удалось разобрать действия", "did_reminder": did_reminder}

    request_id = str(uuid.uuid4())
    plan_dicts = [a.dict() for a in validated]
    for d in plan_dicts:
        d["_chat_id"] = chat_id

    try:
        await agent_manager.send_plan(agent_id, plan_dicts, request_id)
    except Exception as e:
        if notify_telegram:
            await _tg_send_message(chat_id, f"❌ Ошибка отправки плана: {e}")
        return {"ran": False, "ok": False, "error": f"Ошибка отправки: {e}", "did_reminder": did_reminder}

    result = await agent_manager.wait_result(agent_id, request_id, timeout=60)
    if not result.get("success"):
        err = result.get("error", "Unknown error")
        print(f"[DISPATCH] ✗ Agent error: {err}")
        if notify_telegram:
            await _tg_send_message(chat_id, f"⚠️ Ошибка выполнения: {err}")
        return {"ran": True, "ok": False, "error": err, "did_reminder": did_reminder}

    print("[DISPATCH] ✓ Done")
    return {"ran": True, "ok": True, "error": "", "did_reminder": did_reminder}


async def process_text(chat_id: int, text: str) -> dict:
    """
    Полный цикл «сообщение → LLM → ответ + действия» для Mini App API.
    reply в Telegram НЕ шлётся (его показывает Mini App), но действия
    выполняются так же, как в боте. Скриншоты и напоминания при этом
    всё равно приходят в Telegram — это их родной канал.

    Возвращает {"reply": str, "ok": bool, "ran": bool, "error": str}.
    """
    add_to_history(chat_id, "user", text)
    try:
        llm_result = await plan_with_history(
            text, build_system_prompt(), get_history(chat_id)
        )
    except Exception as e:
        import traceback
        print(f"[API] LLM error: {traceback.format_exc()}")
        return {"reply": f"❌ Ошибка LLM: {e}", "ok": False, "ran": False, "error": str(e)}

    reply = llm_result.get("reply", "")
    raw_actions = llm_result.get("actions", [])
    if reply:
        add_to_history(chat_id, "assistant", reply)

    disp = await dispatch_actions(chat_id, raw_actions, notify_telegram=False)
    return {
        "reply": reply,
        "ok": disp.get("ok", True),
        "ran": disp.get("ran", False),
        "error": disp.get("error", ""),
    }


async def _tg_send_message(chat_id, text: str, reply_markup: dict | None = None) -> bool:
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        r = await _tg_client.post(f"{API_BASE}/sendMessage", json=payload)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"[TG] send_message error: {e}")
        return False


async def _tg_send_message_get_id(chat_id, text: str) -> int | None:
    try:
        r = await _tg_client.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    except Exception as e:
        print(f"[TG] send_message_get_id error: {e}")
    return None


async def _tg_delete_message(chat_id, message_id: int | None) -> bool:
    if message_id is None:
        return False
    try:
        r = await _tg_client.post(
            f"{API_BASE}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
        )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"[TG] delete_message error: {e}")
        return False


async def _send_webapp_launcher(chat_id) -> None:
    """Отправляет кнопку запуска Mini App (web_app) в приватный чат."""
    if not WEBAPP_URL:
        await _tg_send_message(
            chat_id,
            "⚙️ Mini App ещё не настроен: задайте <code>WEBAPP_URL</code> в .env "
            "(HTTPS-адрес развёрнутого интерфейса).",
        )
        return
    await _tg_send_message(
        chat_id,
        "🚀 <b>Woyuqe</b> — открой панель управления:",
        reply_markup={
            "inline_keyboard": [
                [{"text": "🖥 Открыть Woyuqe", "web_app": {"url": WEBAPP_URL}}]
            ]
        },
    )


async def _tg_set_menu_button() -> None:
    """
    Ставит кнопку-меню чата (слева от поля ввода) на запуск Mini App —
    чтобы приложение открывалось одним тапом из любого места диалога.
    """
    if not WEBAPP_URL or _tg_client is None:
        return
    try:
        await _tg_client.post(
            f"{API_BASE}/setChatMenuButton",
            json={
                "menu_button": {
                    "type": "web_app",
                    "text": "Woyuqe",
                    "web_app": {"url": WEBAPP_URL},
                }
            },
        )
        print("[BACKEND] ✓ Chat menu button → Mini App")
    except Exception as e:
        print(f"[TG] set_menu_button error: {e}")


async def _tg_send_photo(chat_id, img_bytes: bytes, caption: str = "") -> bool:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{API_BASE}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": ("screenshot.png", img_bytes, "image/png")},
            )
        return r.json().get("ok", False)
    except Exception as e:
        print(f"[TG] send_photo error: {e}")
        return False


@app.get("/health")
async def health():
    llm_status = await check_available()
    return {
        "status": "ok",
        "agents": list(agent_manager._connections.keys()),
        "authorized": list(AUTHORIZED_CHAT_IDS),
        "llm": llm_status,
    }


@app.get("/agents")
async def list_agents():
    return {"agents": list(agent_manager._connections.keys())}


@app.get("/memory/{chat_id}")
async def get_memory(chat_id: int):
    return {"chat_id": chat_id, "history": get_history(chat_id)}


@app.delete("/memory/{chat_id}")
async def clear_memory_api(chat_id: int):
    conversation_history[chat_id] = []
    return {"ok": True}


class ManualPlanRequest(BaseModel):
    text: str
    chat_id: int | str = 0


@app.post("/plan")
async def manual_plan(req: ManualPlanRequest):
    result = await plan_with_history(req.text, build_system_prompt(), [])
    validated = validate_plan(result.get("actions", []))
    return {
        "input": req.text,
        "reply": result.get("reply"),
        "raw_actions": result.get("actions"),
        "validated": [a.dict() for a in validated],
    }


# ── Mini App API ───────────────────────────────────────────────────────
# Роутер /api/* для веб-интерфейса (Telegram Mini App). Регистрируем в самом
# конце модуля — после того как определены dispatch_actions / process_text,
# которые передаются роутеру как зависимости. Так избегаем циклического
# импорта и NameError.
app.include_router(
    create_webapp_router(
        bot_token=BOT_TOKEN,
        authorized_ids=AUTHORIZED_CHAT_IDS,
        executor_path=EXECUTOR_PATH,
        webapp_url=WEBAPP_URL,
        dispatch_actions=lambda cid, acts: dispatch_actions(cid, acts, notify_telegram=False),
        process_text=process_text,
        resolve_site_url=resolve_url,
        dev_chat_id=WEBAPP_DEV_CHAT_ID,
    )
)

# Опционально: раздавать сам Mini App с backend по адресу /app
# (удобно, если не хочется GitHub Pages — тогда WEBAPP_URL = https://host/app/).
_webapp_dir = os.path.join(os.path.dirname(__file__), "..", "webapp")
if os.path.isdir(_webapp_dir):
    from fastapi.staticfiles import StaticFiles

    app.mount("/app", StaticFiles(directory=_webapp_dir, html=True), name="webapp")
