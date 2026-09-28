#!/usr/bin/env python3
"""Московское расписание и надёжная очередь публикаций Telegram."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from database import DatabaseSettings, connect
from lint import hard_violations

MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_SLOTS = (time(10), time(14), time(18), time(21))
MAX_ATTEMPTS = 3
TELEGRAM_LIMIT = 4096

# Отправка, оборвавшаяся после ухода сообщения, но до записи message_id.
# Такую публикацию переотправлять нельзя: пост мог уже уйти в канал.
STUCK_AFTER = "5 minutes"


@dataclass(frozen=True)
class ScheduledPublication:
    id: str
    material_id: str
    title: str
    body: str
    scheduled_at: datetime
    status: str
    destination: str
    attempts: int = 0
    last_error: str | None = None


def moscow_datetime(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=MOSCOW)


def parse_author_date(value: str, now: datetime | None = None) -> date:
    current = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    cleaned = value.strip()
    try:
        if len(cleaned.split(".")) == 2:
            result = datetime.strptime(
                f"{cleaned}.{current.year}", "%d.%m.%Y"
            ).date()
        else:
            result = datetime.strptime(cleaned, "%d.%m.%Y").date()
    except ValueError as error:
        raise ValueError(
            "Напишите дату в формате ДД.ММ или ДД.ММ.ГГГГ"
        ) from error
    if result < current.date():
        raise ValueError("Эта дата уже прошла")
    if result > current.date() + timedelta(days=365):
        raise ValueError("Можно планировать максимум на год вперёд")
    return result


def parse_author_time(value: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as error:
        raise ValueError("Напишите время в формате ЧЧ:ММ, например 18:30") from error


def future_slots(day: date, now: datetime | None = None) -> tuple[time, ...]:
    current = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    return tuple(slot for slot in DEFAULT_SLOTS if moscow_datetime(day, slot) > current)


class PublicationStore:
    def __init__(
        self,
        connection: Any,
        *,
        id_factory: Callable[[], str] | None = None,
    ):
        self.connection = connection
        self.id_factory = id_factory or (lambda: f"pub-{uuid.uuid4().hex}")

    def material_summary(self, material_id: str) -> tuple[str, str, str, str] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.id, t.title, m.format, m.status
                FROM materials AS m JOIN topics AS t ON t.id = m.topic_id
                WHERE m.id = %s
                """,
                (material_id,),
            )
            row = cursor.fetchone()
        return tuple(str(value) for value in row) if row else None

    def schedule(
        self,
        *,
        material_id: str,
        scheduled_at: datetime,
        destination: str = "author",
        publication_id: str | None = None,
        scheduled_by: int | None = None,
        now: datetime | None = None,
    ) -> str:
        current = now or datetime.now(MOSCOW)
        if scheduled_at.tzinfo is None:
            raise ValueError("Время публикации должно содержать часовой пояс")
        if scheduled_at <= current.astimezone(scheduled_at.tzinfo):
            raise ValueError("Время публикации уже прошло")
        if destination not in {"author", "channel"}:
            raise ValueError("Неизвестный получатель публикации")
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT format, status, current_version FROM materials
                    WHERE id = %s FOR UPDATE
                    """,
                    (material_id,),
                )
                material = cursor.fetchone()
                if material is None:
                    raise ValueError("Материал не найден")
                if material[0] != "telegram":
                    raise ValueError("Планировать публикацию можно только для Telegram-поста")
                if material[1] not in {"approved", "scheduled"}:
                    raise ValueError("Сначала материал нужно одобрить")
                if material[1] == "scheduled" and publication_id is None:
                    raise ValueError("Материал уже стоит в очереди")

                version = int(material[2])
                cursor.execute(
                    """
                    SELECT body FROM material_versions
                    WHERE material_id = %s AND version = %s
                    """,
                    (material_id, version),
                )
                body_row = cursor.fetchone()
                if body_row is None:
                    raise ValueError("У материала нет текста")
                body = str(body_row[0])

                # Проверки стоят здесь, а не в момент отправки: там каждая
                # неудача сжигает попытку, и материал за полторы минуты
                # уходил в отказ навсегда без возможности починить из бота.
                if len(body) > TELEGRAM_LIMIT:
                    raise ValueError(
                        f"Текст длиннее {TELEGRAM_LIMIT} знаков — телеграм столько не примет. "
                        "Нажмите «Поправить» и попросите сократить."
                    )
                violations = hard_violations(body, material_id)
                if violations:
                    raise ValueError(
                        "Текст не проходит красные линии: "
                        + "; ".join(item.split("] ", 1)[-1] for item in violations[:3])
                        + ". Публиковать нельзя, нужна правка."
                    )

                if publication_id:
                    cursor.execute(
                        """
                        UPDATE publications
                        SET scheduled_at = %s, destination = %s, status = 'scheduled',
                            attempts = 0, last_error = NULL, locked_at = NULL,
                            material_version = %s, lint_checked_at = now(),
                            scheduled_by = %s
                        WHERE id = %s AND material_id = %s
                          AND status IN ('scheduled', 'failed')
                        """,
                        (
                            scheduled_at,
                            destination,
                            version,
                            scheduled_by,
                            publication_id,
                            material_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Публикацию уже нельзя перенести")
                    resolved_id = publication_id
                else:
                    resolved_id = self.id_factory()
                    cursor.execute(
                        """
                        INSERT INTO publications (
                            id, material_id, platform, scheduled_at, status, destination,
                            material_version, lint_checked_at, scheduled_by
                        ) VALUES (%s, %s, 'telegram', %s, 'scheduled', %s, %s, now(), %s)
                        """,
                        (
                            resolved_id,
                            material_id,
                            scheduled_at,
                            destination,
                            version,
                            scheduled_by,
                        ),
                    )
                cursor.execute(
                    "UPDATE materials SET status = 'scheduled', updated_at = now() WHERE id = %s",
                    (material_id,),
                )
            self.connection.commit()
            return resolved_id
        except Exception:
            self.connection.rollback()
            raise

    def list_active(self) -> tuple[ScheduledPublication, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.id, p.material_id, t.title, mv.body, p.scheduled_at,
                       p.status, p.destination, p.attempts, p.last_error
                FROM publications AS p
                JOIN materials AS m ON m.id = p.material_id
                JOIN topics AS t ON t.id = m.topic_id
                JOIN material_versions AS mv
                  ON mv.material_id = m.id AND mv.version = p.material_version
                WHERE p.status IN ('scheduled', 'publishing', 'failed', 'held')
                ORDER BY p.scheduled_at
                """
            )
            rows = cursor.fetchall()
        return tuple(ScheduledPublication(*row) for row in rows)

    def cancel(self, publication_id: str) -> bool:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE publications SET status = 'cancelled', locked_at = NULL
                    WHERE id = %s AND status IN ('scheduled', 'failed', 'held')
                    RETURNING material_id
                    """,
                    (publication_id,),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        """
                        UPDATE materials SET status = 'approved', updated_at = now()
                        WHERE id = %s
                        """,
                        (row[0],),
                    )
            self.connection.commit()
            return row is not None
        except Exception:
            self.connection.rollback()
            raise

    def claim_due(self) -> ScheduledPublication | None:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT p.id, p.material_id, t.title, mv.body, p.scheduled_at,
                           p.status, p.destination, p.attempts, p.last_error
                    FROM publications AS p
                    JOIN materials AS m ON m.id = p.material_id
                    JOIN topics AS t ON t.id = m.topic_id
                    JOIN material_versions AS mv
                      ON mv.material_id = m.id AND mv.version = p.material_version
                    WHERE p.scheduled_at <= now() AND p.attempts < %s
                      AND p.status IN ('scheduled', 'failed')
                      AND m.status IN ('approved', 'scheduled')
                    ORDER BY p.scheduled_at
                    LIMIT 1 FOR UPDATE OF p SKIP LOCKED
                    """,
                    (MAX_ATTEMPTS,),
                )
                row = cursor.fetchone()
                if row is None:
                    self.connection.rollback()
                    return None
                publication = ScheduledPublication(*row)
                cursor.execute(
                    """
                    UPDATE publications SET status = 'publishing', attempts = attempts + 1,
                        locked_at = now(), last_error = NULL WHERE id = %s
                    """,
                    (publication.id,),
                )
            self.connection.commit()
            return publication
        except Exception:
            self.connection.rollback()
            raise

    def release_stuck(self) -> tuple[str, ...]:
        """Отправки, оборвавшиеся на полпути, переводит в held.

        Между send_message и записью message_id бот мог умереть, и тогда
        неизвестно, ушёл пост в канал или нет. Прежняя версия просто
        забирала такую запись обратно и отправляла ещё раз — до трёх
        дублей в канале. Теперь ждём человека: он смотрит канал и решает.
        """
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE publications
                    SET status = 'held', locked_at = NULL,
                        last_error = 'Отправка прервалась. Проверьте канал: '
                                     'пост мог уйти. Автоматически не повторяю.'
                    WHERE status = 'publishing'
                      AND locked_at < now() - interval '{STUCK_AFTER}'
                    RETURNING id
                    """
                )
                rows = cursor.fetchall()
            self.connection.commit()
            return tuple(str(row[0]) for row in rows)
        except Exception:
            self.connection.rollback()
            raise

    def mark_published(self, publication_id: str, message_id: int) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE publications SET status = 'published', published_at = now(),
                    external_message_id = %s, locked_at = NULL WHERE id = %s
                RETURNING material_id
                """,
                (str(message_id), publication_id),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE materials SET status = 'published', updated_at = now() WHERE id = %s",
                    (row[0],),
                )
        self.connection.commit()

    def mark_failed(self, publication_id: str, error: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE publications SET status = 'failed', last_error = %s,
                    locked_at = NULL WHERE id = %s
                """,
                (error[:1000], publication_id),
            )
        self.connection.commit()


def _store_from_environment() -> tuple[Any, PublicationStore]:
    connection = connect(DatabaseSettings.from_environment())
    return connection, PublicationStore(connection)


def material_summary_from_environment(material_id: str):
    connection, store = _store_from_environment()
    with connection:
        return store.material_summary(material_id)


def schedule_from_environment(**kwargs) -> str:
    connection, store = _store_from_environment()
    with connection:
        return store.schedule(**kwargs)


def list_active_from_environment() -> tuple[ScheduledPublication, ...]:
    connection, store = _store_from_environment()
    with connection:
        return store.list_active()


def cancel_from_environment(publication_id: str) -> bool:
    connection, store = _store_from_environment()
    with connection:
        return store.cancel(publication_id)


def claim_due_from_environment() -> ScheduledPublication | None:
    connection, store = _store_from_environment()
    with connection:
        return store.claim_due()


def release_stuck_from_environment() -> tuple[str, ...]:
    connection, store = _store_from_environment()
    with connection:
        return store.release_stuck()


def mark_published_from_environment(publication_id: str, message_id: int) -> None:
    connection, store = _store_from_environment()
    with connection:
        store.mark_published(publication_id, message_id)


def mark_failed_from_environment(publication_id: str, error: str) -> None:
    connection, store = _store_from_environment()
    with connection:
        store.mark_failed(publication_id, error)
