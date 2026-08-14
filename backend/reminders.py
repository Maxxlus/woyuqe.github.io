"""
backend/reminders.py — LifeOS Reminders v1.0

Хранилище заметок с напоминаниями. Полностью изолировано от Action Protocol
и PC-агента — заметки обрабатываются целиком на backend, на Windows-агент
ничего не уходит.

Логика напоминаний:
  - за 1 час до dt   -> одно сообщение
  - за 15 минут до dt -> одно сообщение
  - в момент dt (или сразу после, если backend был выключен) -> финальное сообщение
  - каждый из трёх флагов отправляется максимум один раз (notified_1h/15m/due)

Хранилище: SQLite-файл reminders.db рядом с этим модулем (можно поменять
путь через переменную окружения REMINDERS_DB_PATH).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

DB_PATH = os.getenv(
    "REMINDERS_DB_PATH",
    os.path.join(os.path.dirname(__file__), "reminders.db"),
)

# Окна срабатывания "за час" / "за 15 минут" — даём допуск в минутах,
# чтобы не пропустить момент, если фоновая проверка идёт раз в минуту.
WINDOW_MINUTES = 1


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _cursor():
    conn = _connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                due_at TEXT NOT NULL,         -- ISO 8601, локальное время сервера
                created_at TEXT NOT NULL,
                notified_1h INTEGER NOT NULL DEFAULT 0,
                notified_15m INTEGER NOT NULL DEFAULT 0,
                notified_due INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0
            )
            """)


@dataclass
class Reminder:
    id: int
    chat_id: int
    text: str
    due_at: datetime
    created_at: datetime
    notified_1h: bool
    notified_15m: bool
    notified_due: bool
    cancelled: bool

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Reminder":
        return Reminder(
            id=row["id"],
            chat_id=row["chat_id"],
            text=row["text"],
            due_at=datetime.fromisoformat(row["due_at"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            notified_1h=bool(row["notified_1h"]),
            notified_15m=bool(row["notified_15m"]),
            notified_due=bool(row["notified_due"]),
            cancelled=bool(row["cancelled"]),
        )


def add_reminder(chat_id: int, text: str, due_at: datetime) -> Reminder:
    now = datetime.now()
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO reminders (chat_id, text, due_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, text, due_at.isoformat(), now.isoformat()),
        )
        new_id = cur.lastrowid
    return Reminder(
        id=new_id,
        chat_id=chat_id,
        text=text,
        due_at=due_at,
        created_at=now,
        notified_1h=False,
        notified_15m=False,
        notified_due=False,
        cancelled=False,
    )


def list_upcoming(chat_id: int) -> list[Reminder]:
    """Все неотменённые заметки с будущим или совсем недавним сроком,
    отсортированные по времени — для команды "что там по заметкам"."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT * FROM reminders
            WHERE chat_id = ? AND cancelled = 0
            ORDER BY due_at ASC
            """,
            (chat_id,),
        )
        rows = cur.fetchall()
    return [Reminder.from_row(r) for r in rows]


def cancel_reminder(reminder_id: int, chat_id: int) -> bool:
    with _cursor() as cur:
        cur.execute(
            "UPDATE reminders SET cancelled = 1 WHERE id = ? AND chat_id = ?",
            (reminder_id, chat_id),
        )
        return cur.rowcount > 0


def _due_for_notification() -> list[tuple[Reminder, str]]:
    """
    Возвращает список (reminder, stage), где stage — один из
    "1h" / "15m" / "due", для которых пора отправить сообщение.
    Не помечает их как отправленные — это делает вызывающий код
    после успешной отправки в Telegram.
    """
    now = datetime.now()
    result: list[tuple[Reminder, str]] = []

    with _cursor() as cur:
        cur.execute("SELECT * FROM reminders WHERE cancelled = 0")
        rows = cur.fetchall()

    for row in rows:
        r = Reminder.from_row(row)
        delta = r.due_at - now

        # "due" — момент настал (или уже прошёл, если backend был выключен)
        if not r.notified_due and delta <= timedelta(minutes=0):
            result.append((r, "due"))
            continue  # если уже наступил момент — промежуточные не шлём отдельно

        # "15m" — осталось ~15 минут
        if not r.notified_15m and timedelta(
            minutes=15 - WINDOW_MINUTES
        ) <= delta <= timedelta(minutes=15 + WINDOW_MINUTES):
            result.append((r, "15m"))

        # "1h" — остался ~1 час
        if not r.notified_1h and timedelta(
            minutes=60 - WINDOW_MINUTES
        ) <= delta <= timedelta(minutes=60 + WINDOW_MINUTES):
            result.append((r, "1h"))

    return result


def mark_notified(reminder_id: int, stage: str) -> None:
    column = {"1h": "notified_1h", "15m": "notified_15m", "due": "notified_due"}[stage]
    with _cursor() as cur:
        cur.execute(f"UPDATE reminders SET {column} = 1 WHERE id = ?", (reminder_id,))


def get_due_notifications() -> list[tuple[Reminder, str]]:
    """Публичная точка входа для фоновой задачи в main.py."""
    return _due_for_notification()
