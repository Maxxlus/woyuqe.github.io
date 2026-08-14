"""
protocol/actions.py — LifeOS Action Protocol v2.1

ДОБАВЛЕНО:
  - close_tab — закрыть конкретную вкладку браузера по части названия
  - SYSTEM_PROMPT обновлён под новый формат {"reply":..., "actions":[...]}
    и описывает работу с координатами по скриншоту с разметкой
"""

from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field


class OpenAppAction(BaseModel):
    action: Literal["open_app"]
    app: str


class CloseAppAction(BaseModel):
    action: Literal["close_app"]
    app: str


class CloseTabAction(BaseModel):
    """
    Закрывает вкладку браузера, заголовок которой содержит указанный текст.
    Работает через: Ctrl+Shift+A (список вкладок Chrome) → поиск по OCR →
    клик на найденную вкладку → закрытие (или Ctrl+W если это активная вкладка).
    """

    action: Literal["close_tab"]
    title_contains: str = Field(
        ..., description="Часть заголовка вкладки, например 'YouTube'"
    )


class ClickAction(BaseModel):
    action: Literal["click"]
    x: int
    y: int


class DoubleClickAction(BaseModel):
    action: Literal["double_click"]
    x: int
    y: int


class RightClickAction(BaseModel):
    action: Literal["right_click"]
    x: int
    y: int


class MoveMouseAction(BaseModel):
    action: Literal["move_mouse"]
    x: int
    y: int


class ScrollAction(BaseModel):
    action: Literal["scroll"]
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = 300


class PressAction(BaseModel):
    action: Literal["press"]
    key: str


class HotkeyAction(BaseModel):
    action: Literal["hotkey"]
    keys: list[str]


class TypeAction(BaseModel):
    action: Literal["type"]
    text: str


class WaitAction(BaseModel):
    action: Literal["wait"]
    seconds: float = 1.0


class ScreenshotAction(BaseModel):
    action: Literal["screenshot"]
    send_to_chat: bool = True


class FindTextAction(BaseModel):
    action: Literal["find_text"]
    text: str


class FindImageAction(BaseModel):
    action: Literal["find_image"]
    image: str
    click_on_found: bool = False


class SetVolumeAction(BaseModel):
    action: Literal["set_volume"]
    percent: int = Field(..., ge=0, le=100)


class SayAction(BaseModel):
    action: Literal["say"]
    text: str


class OpenUrlAction(BaseModel):
    action: Literal["open_url"]
    url: str


class GetClipboardAction(BaseModel):
    action: Literal["get_clipboard"]


class SetClipboardAction(BaseModel):
    action: Literal["set_clipboard"]
    text: str


class AddReminderAction(BaseModel):
    """
    Заметка с напоминанием. Обрабатывается ПОЛНОСТЬЮ на backend
    (см. backend/reminders.py) — на PC-агент это действие никогда
    не отправляется.
    """

    action: Literal["add_reminder"]
    text: str = Field(..., description="Текст заметки, например 'пойти гулять'")
    due_at: str = Field(
        ...,
        description="Дата и время в формате ISO 8601, например '2026-06-23T13:00:00'",
    )


class ListRemindersAction(BaseModel):
    """Запрос списка предстоящих заметок. Обрабатывается на backend."""

    action: Literal["list_reminders"]


class CancelReminderAction(BaseModel):
    """Отмена заметки по её id (id показывается в списке заметок)."""

    action: Literal["cancel_reminder"]
    reminder_id: int


ACTION_REGISTRY: dict[str, type[BaseModel]] = {
    "open_app": OpenAppAction,
    "close_app": CloseAppAction,
    "close_tab": CloseTabAction,
    "click": ClickAction,
    "double_click": DoubleClickAction,
    "right_click": RightClickAction,
    "move_mouse": MoveMouseAction,
    "scroll": ScrollAction,
    "press": PressAction,
    "hotkey": HotkeyAction,
    "type": TypeAction,
    "wait": WaitAction,
    "screenshot": ScreenshotAction,
    "find_text": FindTextAction,
    "find_image": FindImageAction,
    "set_volume": SetVolumeAction,
    "say": SayAction,
    "open_url": OpenUrlAction,
    "get_clipboard": GetClipboardAction,
    "set_clipboard": SetClipboardAction,
    "add_reminder": AddReminderAction,
    "list_reminders": ListRemindersAction,
    "cancel_reminder": CancelReminderAction,
}


def validate_action(raw: dict) -> BaseModel | None:
    action_type = raw.get("action")
    model_cls = ACTION_REGISTRY.get(action_type)
    if model_cls is None:
        return None
    return model_cls(**raw)


def validate_plan(raw_list: list[dict]) -> list[BaseModel]:
    result = []
    for i, raw in enumerate(raw_list):
        action = validate_action(raw)
        if action is None:
            print(f"[PROTOCOL] Неизвестный action #{i}: {raw.get('action')} — пропущен")
        else:
            result.append(action)
    return result


# ── Системный промпт ────────────────────────────────────────────────────


def build_system_prompt(now_iso: str | None = None) -> str:
    if now_iso is None:
        from datetime import datetime
        now_iso = datetime.now().isoformat(timespec="seconds")

    return f"""Ты Woyuqe — ИИ-помощник для Windows. Отвечай ТОЛЬКО JSON:
{{"reply":"текст пользователю","actions":[...]}}
reply всегда заполнен. actions пустой если просто разговор. Без markdown.

Сейчас: {now_iso} — используй для расчёта "завтра", "через час" и т.п.
Экран 1920x1080. Если прислан скриншот с обведённой областью → {{"action":"click","x":N,"y":N}}

Действия:
open_app{{app}} close_app{{app}} close_tab{{title_contains}} open_url{{url}}
click{{x,y}} double_click{{x,y}} right_click{{x,y}} move_mouse{{x,y}} scroll{{direction,amount}}
press{{key: enter,esc,tab,space,f5,next_track,prev_track,play_pause,media_stop}}
hotkey{{keys[]}} type{{text}} wait{{seconds}} screenshot{{send_to_chat}}
set_volume{{percent}} get_clipboard set_clipboard{{text}}
add_reminder{{text,due_at: YYYY-MM-DDTHH:MM:SS}} list_reminders cancel_reminder{{reminder_id}}

close_tab — одна вкладка по части заголовка. close_app — всё приложение.

Пример: "следующий трек" → {{"reply":"Следующий трек","actions":[{{"action":"press","key":"next_track"}}]}}
Пример: "заметка завтра 13:00 погулять" → {{"reply":"Поставил","actions":[{{"action":"add_reminder","text":"погулять","due_at":"2026-06-23T13:00:00"}}]}}"""


# Сохраняем как константу для обратной совместимости с кодом, который
# импортирует SYSTEM_PROMPT напрямую — но она "замороженная" на момент
# импорта модуля. Используйте build_system_prompt() для актуальной даты.
SYSTEM_PROMPT = build_system_prompt()
