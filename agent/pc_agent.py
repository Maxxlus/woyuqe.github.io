"""
agent/pc_agent.py — Woyuqe v3.0

НАСТОЯЩАЯ ПРИЧИНА no running event loop:
  asyncio.Queue.put_nowait() сам по себе НЕ thread-safe.
  Внутри put_nowait Queue может вызвать self._wakeup_next(), который
  использует call_soon() БЕЗ _threadsafe — а call_soon() без _threadsafe
  требует running event loop В ТЕКУЩЕМ потоке. Поток Executor (to_thread)
  не имеет event loop вообще — отсюда "no running event loop".

ИСПРАВЛЕНИЕ:
  loop.call_soon_threadsafe(outbox.put_nowait, item)
  call_soon_threadsafe — единственный по-настоящему thread-safe способ
  взаимодействия с объектами event loop (включая Queue) из другого потока.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import uuid
import traceback

current_file_dir = os.path.dirname(os.path.abspath(__file__))
project_root_dir = os.path.dirname(current_file_dir)

if project_root_dir not in sys.path:
    sys.path.insert(0, project_root_dir)
if current_file_dir not in sys.path:
    sys.path.insert(0, current_file_dir)

try:
    import websockets
    from dotenv import load_dotenv
except ImportError:
    print("\n[КРИТИЧЕСКАЯ ОШИБКА] Выполни: pip install websockets python-dotenv")
    sys.exit(1)

try:
    from executor import Executor
    from protocol.actions import validate_plan
except ImportError:
    try:
        from agent.executor import Executor
        from agent.protocol.actions import validate_plan
    except ImportError as e:
        print(f"\n[КРИТИЧЕСКАЯ ОШИБКА] Не удалось импортировать Executor: {e}")
        print(traceback.format_exc())
        sys.exit(1)

load_dotenv()

BACKEND_URL = os.getenv("BACKEND_URL", "ws://localhost:8000")
AGENT_ID = os.getenv("AGENT_ID", f"agent-{uuid.uuid4().hex[:8]}")
RECONNECT_DELAY = 5

AUTHORIZED_CHAT_IDS: set[int] = {
    int(cid.strip())
    for cid in os.getenv("AUTHORIZED_CHAT_IDS", "1616991465").split(",")
    if cid.strip().isdigit()
}


def is_authorized(chat_id) -> bool:
    if chat_id is None:
        return False
    try:
        return int(chat_id) in AUTHORIZED_CHAT_IDS
    except (ValueError, TypeError):
        return False


_state = {"chat_id": None}


def current_chat_id():
    return _state.get("chat_id")


async def main():
    print(f"=========================================")
    print(f"  Woyuqe PC Agent v3.0 STARTED")
    print(f"  AGENT_ID: {AGENT_ID}")
    print(f"  PROJECT ROOT: {project_root_dir}")
    print(f"  AUTHORIZED: {AUTHORIZED_CHAT_IDS}")
    print(f"=========================================")

    while True:
        try:
            await connect_and_run()
        except Exception as e:
            print(
                f"[AGENT] Соединение разорвано: {e}. Повтор через {RECONNECT_DELAY} сек..."
            )
            await asyncio.sleep(RECONNECT_DELAY)


async def connect_and_run():
    uri = f"{BACKEND_URL}/ws/agent/{AGENT_ID}"
    async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
        print("[AGENT] ✓ Подключено к бэкенду. Ожидаю команды...")

        # ИСПРАВЛЕНИЕ: получаем ссылку на текущий running loop ЗАРАНЕЕ,
        # пока мы точно в основном async потоке
        main_loop = asyncio.get_running_loop()
        outbox: asyncio.Queue = asyncio.Queue()

        def safe_say(text):
            """
            Вызывается из потока Executor (asyncio.to_thread).
            call_soon_threadsafe — ЕДИНСТВЕННЫЙ безопасный способ
            положить элемент в Queue из чужого потока.
            """
            item = {
                "type": "say",
                "chat_id": current_chat_id(),
                "text": f"🤖 {text}",
            }
            main_loop.call_soon_threadsafe(outbox.put_nowait, item)

        def safe_screenshot(img):
            item = {
                "type": "screenshot",
                "chat_id": current_chat_id(),
                "image_b64": base64.b64encode(img).decode(),
                "caption": "📸 Скриншот экрана",
            }
            main_loop.call_soon_threadsafe(outbox.put_nowait, item)

        executor = Executor(on_say=safe_say, on_screenshot=safe_screenshot)
        _state["chat_id"] = None

        await asyncio.gather(
            read_loop(ws, executor),
            send_loop(ws, outbox),
        )


async def read_loop(ws, executor: Executor):
    """Читает входящие планы от backend."""
    async for raw_msg in ws:
        try:
            msg = json.loads(raw_msg)
            if msg.get("type") == "plan":
                asyncio.create_task(handle_plan(ws, executor, msg))
            elif msg.get("type") == "pong":
                pass
        except Exception as e:
            print(f"[AGENT] Ошибка парсинга сообщения: {e}")


async def send_loop(ws, outbox: asyncio.Queue):
    """
    Читает outbox и отправляет в WebSocket.
    Работает ИСКЛЮЧИТЕЛЬНО в основном async потоке.
    """
    while True:
        payload = await outbox.get()
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            print(f"[AGENT] Ошибка отправки из outbox: {e}")


async def handle_plan(ws, executor: Executor, msg: dict):
    request_id = msg.get("request_id", "unknown")
    raw_actions = msg.get("actions", [])

    chat_id = None
    for a in raw_actions:
        if "_chat_id" in a:
            chat_id = a.pop("_chat_id")
            break

    if not is_authorized(chat_id):
        print(f"[AUTH] ⛔ Отклонено: chat_id={chat_id}")
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "success": False,
                        "error": f"Unauthorized: {chat_id}",
                    }
                )
            )
        except Exception:
            pass
        return

    _state["chat_id"] = chat_id

    validated = validate_plan(raw_actions)
    if not validated:
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "result",
                        "request_id": request_id,
                        "success": False,
                        "error": "Empty or invalid plan",
                    }
                )
            )
        except Exception:
            pass
        return

    print(f"[AGENT] Выполняю: {[a.action for a in validated]}")
    results = await executor.run_plan(validated)

    failed = [r for r in results if not r.success]
    success = len(failed) == 0

    try:
        await ws.send(
            json.dumps(
                {
                    "type": "result",
                    "request_id": request_id,
                    "success": success,
                    "error": "; ".join(r.output for r in failed) if failed else "",
                    "results": [r.dict(exclude={"screenshot"}) for r in results],
                },
                ensure_ascii=False,
            )
        )
        print(f"[AGENT] ✓ Результат отправлен. Успех={success}")
    except Exception as e:
        print(f"[AGENT] Не удалось отправить статус: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[AGENT] Остановлено пользователем.")
