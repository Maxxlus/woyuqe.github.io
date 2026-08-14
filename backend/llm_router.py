"""
backend/llm_router.py — Woyuqe LLM Router v3.4 (OpenRouter)

Переход с Gemini на OpenRouter.
OpenRouter использует OpenAI-совместимый формат запросов (messages),
а не формат Gemini (contents/parts).

.env:
  OPENROUTER_API_KEY=sk-or-v1-...
  OPENROUTER_MODEL=openai/gpt-5.4-mini
"""

from __future__ import annotations

import json
import re
import httpx
import os
import traceback
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions"
)
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-5.4-mini")
TIMEOUT = 60


async def plan_with_history(
    user_text: str,
    system_prompt: str,
    history: list[dict],
) -> dict:
    """
    Возвращает {"reply": str, "actions": list[dict]}.
    """
    try:
        raw = await _openrouter_chat(system_prompt, user_text, history)

        if not raw:
            print("[LLM] ⚠️ Пустой ответ от OpenRouter")
            return {
                "reply": "Модель вернула пустой ответ. Попробуйте ещё раз.",
                "actions": [],
            }

        parsed = _try_parse_json(raw)
        if parsed is not None:
            return parsed

        print(f"[LLM] ⚠️ Невалидный JSON, повторяю запрос. Raw: {raw[:150]}")
        retry_prompt = (
            f"{user_text}\n\n"
            f"ВАЖНО: ответь СТРОГО валидным JSON-объектом вида "
            f'{{"reply": "...", "actions": [...]}}, без markdown, без текста вокруг.'
        )
        raw_retry = await _openrouter_chat(system_prompt, retry_prompt, history)
        parsed_retry = _try_parse_json(raw_retry)
        if parsed_retry is not None:
            return parsed_retry

        print(f"[LLM] ✗ Повтор тоже не дал JSON. Raw retry: {raw_retry[:150]}")
        return {
            "reply": "Не удалось обработать команду, попробуйте переформулировать.",
            "actions": [],
        }

    except Exception as e:
        print(f"[LLM] ❌ Ошибка в plan_with_history: {e}")
        print(traceback.format_exc())
        return {"reply": f"Ошибка LLM: {e}", "actions": []}


async def plan(user_text: str, system_prompt: str) -> dict:
    return await plan_with_history(user_text, system_prompt, [])


async def resolve_url(query: str) -> str:
    """
    Превращает НАЗВАНИЕ сайта (например «ютуб», «гитхаб», «вк») в готовый
    URL для открытия в браузере. Использует ту же LLM (OpenRouter).
    Всегда возвращает валидный https-URL — при любой ошибке откатывается
    на поиск Google, чтобы действие open_url не падало.
    """
    import re
    from urllib.parse import quote

    system = (
        "Ты преобразуешь короткое название сайта или сервиса в один "
        "готовый URL для открытия в браузере. Выбирай самый очевидный "
        "официальный сайт. Отвечай СТРОГО валидным JSON без markdown: "
        '{"url":"https://..."} — только поле url, без пояснений.'
    )
    user = f"Название: {query}"

    try:
        raw = await _openrouter_chat(system, user, [])
        url = None
        try:
            obj = json.loads((raw or "").strip().strip("`"))
            if isinstance(obj, dict) and obj.get("url"):
                url = str(obj["url"]).strip()
        except Exception:
            pass

        if not url:
            m = re.search(r"https?://[^\s\"'<>]+", raw or "")
            if m:
                url = m.group(0)

        if url:
            url = url.strip().strip('"').strip("'")
            if not url.startswith("http"):
                url = "https://" + url.lstrip("/")
            return url
    except Exception as e:
        print(f"[LLM] resolve_url error: {e}")

    return "https://www.google.com/search?q=" + quote(query)


async def check_available() -> dict:
    if not OPENROUTER_KEY:
        return {
            "provider": "openrouter",
            "status": "error",
            "error": "OPENROUTER_API_KEY отсутствует в .env",
        }
    return {"provider": "openrouter", "status": "ok", "model": OPENROUTER_MODEL}


async def _openrouter_chat(system: str, user: str, history: list[dict]) -> str:
    """
    OpenRouter использует OpenAI-совместимый формат:
    {"model": ..., "messages": [{"role": ..., "content": ...}]}
    """
    messages = [{"role": "system", "content": system}]

    # История (без последнего сообщения — оно будет добавлено как текущее)
    if history:
        messages.extend(history[:-1])

    messages.append({"role": "user", "content": user})

    headers = {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        # OpenRouter рекомендует указывать источник запроса (не обязательно, но полезно для рейтинг-лимитов)
        "HTTP-Referer": "https://lifeos.local",
        "X-Title": "LifeOS",
    }

    payload = {
        "model": OPENROUTER_MODEL,
        "temperature": 0.1,
        "messages": messages,
        "response_format": {
            "type": "json_object"
        },  # просим строго JSON, если модель поддерживает
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(OPENROUTER_URL, json=payload, headers=headers)

        if r.status_code >= 400:
            print(f"[LLM] ✗ OpenRouter HTTP {r.status_code}: {r.text[:300]}")
            r.raise_for_status()

        resp_data = r.json()
        try:
            return resp_data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            print(
                f"[LLM] Нетипичный ответ OpenRouter: {json.dumps(resp_data, ensure_ascii=False)[:300]}"
            )
            return ""


def _try_parse_json(raw: str) -> dict | None:
    """
    Парсит JSON-объект формата {"reply": str, "actions": list}.
    Возвращает None если не удалось распарсить.
    """
    if not raw:
        return None

    raw = raw.strip()
    if raw.startswith("```"):
        lines = [l for l in raw.split("\n") if not l.startswith("```")]
        raw = "\n".join(lines).strip()

    result = None
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None

    if result is None:
        return None

    if isinstance(result, dict) and "reply" in result:
        actions = result.get("actions", [])
        if not isinstance(actions, list):
            actions = []
        return {"reply": str(result.get("reply", "")), "actions": actions}

    # Обратная совместимость со старым плоским форматом
    if isinstance(result, list):
        say_items = [
            a for a in result if isinstance(a, dict) and a.get("action") == "say"
        ]
        reply = say_items[0]["text"] if say_items else ""
        return {"reply": reply, "actions": result}

    return None
