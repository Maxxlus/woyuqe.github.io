"""
backend/webapp_api.py — Woyuqe Mini App API v1.0

REST-эндпоинты для Telegram Mini App (веб-интерфейс бота).

Аутентификация — та же, что и в боте: по Telegram chat_id.
Mini App присылает `Telegram.WebApp.initData` в заголовке
`X-Telegram-Init-Data`; здесь она проверяется HMAC-подписью бота
(секрет = BOT_TOKEN) — подделать нельзя. Из подписанных данных
достаётся user.id и сверяется с AUTHORIZED_CHAT_IDS.

Эндпоинты (все под /api):
  GET  /api/config     — публичный конфиг (есть ли WEBAPP и т.п.)
  GET  /api/apps       — список приложений (из executor.py APP_MAP, без дублей)
  POST /api/open_app   — открыть приложение на ПК
  POST /api/open_site  — открыть сайт по названию (ИИ генерирует URL)
  POST /api/ask        — свободный запрос к ИИ (как сообщение боту)

Модуль НЕ импортирует executor.py напрямую (там pyautogui / pycaw —
Windows-only). APP_MAP парсится из исходника через ast, поэтому backend
спокойно работает и на Linux/VPS.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import time
from typing import Callable, Awaitable
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel


# ────────────────────────── парсинг APP_MAP ──────────────────────────

def parse_app_map(executor_path: str) -> dict[str, str]:
    """
    Достаёт словарь APP_MAP из agent/executor.py БЕЗ импорта модуля
    (чтобы не тянуть pyautogui/pycaw на сервере). Читаем исходник и
    вычисляем литерал через ast.literal_eval.
    """
    try:
        with open(executor_path, "r", encoding="utf-8") as f:
            src = f.read()
    except OSError:
        return {}

    tree = ast.parse(src)
    for node in ast.walk(tree):
        # APP_MAP объявлен с аннотацией: `APP_MAP: dict[str, str] = {...}`
        # (ast.AnnAssign), но поддержим и обычное присваивание (ast.Assign).
        targets = []
        value = None
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value

        for target in targets:
            if isinstance(target, ast.Name) and target.id == "APP_MAP":
                try:
                    parsed = ast.literal_eval(value)
                    if isinstance(parsed, dict):
                        return {str(k): str(v) for k, v in parsed.items()}
                except (ValueError, SyntaxError):
                    return {}
    return {}


def dedupe_apps(app_map: dict[str, str]) -> list[str]:
    """
    Схлопывает дубли: несколько ключей с одинаковым путём (например
    'yandex music' / 'яндекс музыка' или 'v2raytun' / 'впн' / 'vpn')
    считаются одним приложением. Возвращает список канонических ключей
    в порядке первого появления пути. Каноническим предпочитается
    ASCII-ключ (латиница) — с ним удобнее работать фронту.
    """
    canonical: dict[str, str] = {}   # normalized path -> chosen key
    order: list[str] = []            # normalized paths, first-seen order

    for key, path in app_map.items():
        norm = os.path.expandvars(path).strip().lower()
        if norm not in canonical:
            canonical[norm] = key
            order.append(norm)
        else:
            existing = canonical[norm]
            # если уже выбран не-ASCII ключ, а текущий ASCII — заменяем
            if not existing.isascii() and key.isascii():
                canonical[norm] = key

    return [canonical[norm] for norm in order]


# ────────────────────────── проверка initData ─────────────────────────

def verify_init_data(bot_token: str, init_data: str, max_age_sec: int = 86400) -> dict | None:
    """
    Проверяет подпись Telegram WebApp initData.
    Возвращает dict пользователя (user) при успехе, иначе None.

    Алгоритм — из документации Telegram:
      secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
      hash       = HMAC_SHA256(key=secret_key, msg=data_check_string)
    где data_check_string — все поля (кроме hash), отсортированные по ключу,
    в виде "k=v", склеенные через '\\n'.
    """
    if not init_data or not bot_token:
        return None

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        return None

    # свежесть (защита от повторного использования старой initData)
    if max_age_sec > 0:
        try:
            auth_date = int(pairs.get("auth_date", "0"))
            if auth_date and (time.time() - auth_date) > max_age_sec:
                return None
        except ValueError:
            pass

    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except Exception:
        return None


# ────────────────────────── модели запросов ───────────────────────────

class OpenAppRequest(BaseModel):
    app: str


class OpenSiteRequest(BaseModel):
    query: str


class AskRequest(BaseModel):
    text: str


# ────────────────────────── фабрика роутера ───────────────────────────

def create_webapp_router(
    *,
    bot_token: str,
    authorized_ids: set[int],
    executor_path: str,
    webapp_url: str,
    dispatch_actions: Callable[[int, list[dict]], Awaitable[dict]],
    process_text: Callable[[int, str], Awaitable[dict]],
    resolve_site_url: Callable[[str], Awaitable[str]],
    dev_chat_id: int | None = None,
) -> APIRouter:
    """
    Собирает APIRouter. Зависимости прокидываются из main.py, чтобы
    не было циклического импорта и чтобы роутер переиспользовал уже
    существующие agent_manager / историю / LLM.

    dev_chat_id — если задан (env WEBAPP_DEV_CHAT_ID), позволяет
    тестировать API в обычном браузере без Telegram (initData пустой).
    Держите его пустым в проде.
    """
    router = APIRouter(prefix="/api")

    def auth(init_data: str | None) -> int:
        """Возвращает авторизованный chat_id или бросает 401/403."""
        if not init_data:
            if dev_chat_id is not None:
                return dev_chat_id
            raise HTTPException(status_code=401, detail="No initData")
        user = verify_init_data(bot_token, init_data)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid initData signature")
        chat_id = int(user.get("id", 0))
        if chat_id not in authorized_ids:
            raise HTTPException(status_code=403, detail="Доступ запрещён")
        return chat_id

    def _default_label(key: str) -> str:
        return key.replace(".", " ").replace("_", " ").strip().title()

    @router.get("/config")
    async def config():
        return {
            "app": "Woyuqe",
            "webapp_configured": bool(webapp_url),
            "dev_mode": dev_chat_id is not None,
        }

    @router.get("/apps")
    async def apps(x_telegram_init_data: str | None = Header(default=None)):
        auth(x_telegram_init_data)
        app_map = parse_app_map(executor_path)
        keys = dedupe_apps(app_map)
        return {
            "apps": [{"key": k, "label": _default_label(k)} for k in keys]
        }

    @router.post("/open_app")
    async def open_app(
        body: OpenAppRequest,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        chat_id = auth(x_telegram_init_data)
        app_map = parse_app_map(executor_path)
        if body.app not in app_map:
            raise HTTPException(status_code=404, detail=f"Неизвестное приложение: {body.app}")
        result = await dispatch_actions(chat_id, [{"action": "open_app", "app": body.app}])
        if not result.get("ok"):
            raise HTTPException(status_code=502, detail=result.get("error", "Agent error"))
        return {"ok": True, "app": body.app}

    @router.post("/open_site")
    async def open_site(
        body: OpenSiteRequest,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        chat_id = auth(x_telegram_init_data)
        query = body.query.strip()
        if not query:
            raise HTTPException(status_code=400, detail="Пустой запрос")
        url = await resolve_site_url(query)
        result = await dispatch_actions(chat_id, [{"action": "open_url", "url": url}])
        if not result.get("ok"):
            raise HTTPException(status_code=502, detail=result.get("error", "Agent error"))
        return {"ok": True, "url": url, "query": query}

    @router.post("/ask")
    async def ask(
        body: AskRequest,
        x_telegram_init_data: str | None = Header(default=None),
    ):
        chat_id = auth(x_telegram_init_data)
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Пустой запрос")
        result = await process_text(chat_id, text)
        return result

    return router
