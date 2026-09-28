#!/usr/bin/env python3
"""Учёт расходов AI Gateway и месячный предохранитель бюджета."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from threading import Lock
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

MILLION = Decimal("1000000")
MONEY_QUANTUM = Decimal("0.0001")
MOSCOW = ZoneInfo("Europe/Moscow")

log = logging.getLogger(__name__)


class AIBudgetConfigurationError(ValueError):
    """Настройки бюджета отсутствуют или противоречат друг другу."""


class AIBudgetExceeded(RuntimeError):
    """Новый запрос не помещается в жёсткий месячный лимит."""

    AUTHOR_MESSAGE = (
        "Лимит на генерации в этом месяце закончился. "
        "Напиши Сане — он проверит баланс AI Gateway и при необходимости пополнит его."
    )

    def __init__(self, technical_details: str):
        self.technical_details = technical_details
        super().__init__(self.AUTHOR_MESSAGE)


@dataclass(frozen=True)
class TokenPricing:
    input_rub_per_million: Decimal
    output_rub_per_million: Decimal

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Количество токенов не может быть отрицательным")
        value = (
            Decimal(input_tokens) * self.input_rub_per_million
            + Decimal(output_tokens) * self.output_rub_per_million
        ) / MILLION
        return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class BudgetSettings:
    warning_rub: Decimal
    hard_limit_rub: Decimal
    fast_pricing: TokenPricing
    writer_pricing: TokenPricing

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "BudgetSettings":
        def money(name: str, default: str) -> Decimal:
            raw = values.get(name, default).strip()
            try:
                parsed = Decimal(raw)
            except InvalidOperation as error:
                raise AIBudgetConfigurationError(f"{name} должен быть числом") from error
            if not parsed.is_finite() or parsed <= 0:
                raise AIBudgetConfigurationError(f"{name} должен быть больше нуля")
            return parsed

        warning = money("AI_BUDGET_WARNING_RUB", "350")
        hard_limit = money("AI_BUDGET_HARD_LIMIT_RUB", "500")
        if warning >= hard_limit:
            raise AIBudgetConfigurationError(
                "AI_BUDGET_WARNING_RUB должен быть меньше AI_BUDGET_HARD_LIMIT_RUB"
            )
        return cls(
            warning_rub=warning,
            hard_limit_rub=hard_limit,
            fast_pricing=TokenPricing(
                money("AI_PRICE_FAST_INPUT_RUB_PER_MILLION", "27"),
                money("AI_PRICE_FAST_OUTPUT_RUB_PER_MILLION", "162"),
            ),
            writer_pricing=TokenPricing(
                money("AI_PRICE_WRITER_INPUT_RUB_PER_MILLION", "270"),
                money("AI_PRICE_WRITER_OUTPUT_RUB_PER_MILLION", "1620"),
            ),
        )

    @classmethod
    def from_environment(cls) -> "BudgetSettings":
        return cls.from_mapping(os.environ)

    def pricing_for(self, task: str) -> TokenPricing:
        if task == "fast":
            return self.fast_pricing
        if task == "writer":
            return self.writer_pricing
        raise ValueError("task должен быть 'fast' или 'writer'")


@dataclass(frozen=True)
class BudgetState:
    spent_rub: Decimal
    warning_rub: Decimal
    hard_limit_rub: Decimal

    @property
    def warning_reached(self) -> bool:
        return self.spent_rub >= self.warning_rub

    @property
    def blocked(self) -> bool:
        return self.spent_rub >= self.hard_limit_rub

    @property
    def remaining_rub(self) -> Decimal:
        return max(Decimal("0"), self.hard_limit_rub - self.spent_rub)


class UsageStore(Protocol):
    def monthly_spend(self, month_start: datetime, next_month_start: datetime) -> Decimal: ...

    def record(
        self,
        *,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_rub: Decimal,
    ) -> int: ...

    def settle(
        self,
        usage_id: int,
        *,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_rub: Decimal,
    ) -> None: ...

    def mark(self, usage_id: int, *, operation: str) -> None: ...


class PostgresUsageStore:
    """Хранилище расходов в таблице ``ai_usage``."""

    def __init__(self, connection_factory: Callable[[], Any]):
        self.connection_factory = connection_factory

    def monthly_spend(self, month_start: datetime, next_month_start: datetime) -> Decimal:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_rub), 0)
                FROM ai_usage
                WHERE created_at >= %s AND created_at < %s
                """,
                (month_start, next_month_start),
            )
            row = cursor.fetchone()
        return Decimal(str(row[0] or 0))

    def record(
        self,
        *,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_rub: Decimal,
    ) -> int:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_usage (
                    operation, model, input_tokens, output_tokens, estimated_cost_rub
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (operation, model, input_tokens, output_tokens, estimated_cost_rub),
            )
            row = cursor.fetchone()
            connection.commit()
        return int(row[0]) if row else 0

    def settle(
        self,
        usage_id: int,
        *,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_rub: Decimal,
    ) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_usage
                SET operation = %s, model = %s, input_tokens = %s,
                    output_tokens = %s, estimated_cost_rub = %s
                WHERE id = %s
                """,
                (
                    operation,
                    model,
                    input_tokens,
                    output_tokens,
                    estimated_cost_rub,
                    usage_id,
                ),
            )
            connection.commit()


    def mark(self, usage_id: int, *, operation: str) -> None:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ai_usage SET operation = %s WHERE id = %s",
                (operation, usage_id),
            )
            connection.commit()


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        next_start = start.replace(year=start.year + 1, month=1)
    else:
        next_start = start.replace(month=start.month + 1)
    return start, next_start


class AIBudgetGuard:
    """Проверяет лимит до запроса и сохраняет фактический расход после него."""

    def __init__(self, settings: BudgetSettings, store: UsageStore):
        self.settings = settings
        self.store = store
        # Бот работает одним процессом; lock не даёт двум обработчикам одновременно
        # пройти проверку по одному и тому же остатку бюджета.
        self.lock = Lock()

    def state(self, now: datetime | None = None) -> BudgetState:
        start, next_start = month_bounds(now)
        return BudgetState(
            spent_rub=self.store.monthly_spend(start, next_start),
            warning_rub=self.settings.warning_rub,
            hard_limit_rub=self.settings.hard_limit_rub,
        )

    def reserve(
        self, *, task: str, input_tokens: int, max_output_tokens: int
    ) -> tuple[int, BudgetState]:
        """Проверить лимит и сразу занять худший допустимый расход.

        Проверка без записи лимит не держала: между ней и записью факта
        проходил весь HTTP-вызов, до нескольких минут, и параллельные
        запросы — джоба бота и CLI — видели одну и ту же сумму. Блокировка
        внутри процесса от этого не спасала: процессов минимум два.
        """
        with self.lock:
            state = self.state()
            reserved = self.settings.pricing_for(task).cost(input_tokens, max_output_tokens)
            if state.blocked or state.spent_rub + reserved > state.hard_limit_rub:
                raise AIBudgetExceeded(
                    "Технические данные: "
                    f"потрачено {state.spent_rub:.2f} ₽, "
                    f"запросу нужно до {reserved:.2f} ₽, "
                    f"лимит {state.hard_limit_rub:.2f} ₽"
                )
            usage_id = self.store.record(
                operation=f"{task}:reserved",
                model="—",
                input_tokens=input_tokens,
                output_tokens=max_output_tokens,
                estimated_cost_rub=reserved,
            )
            return usage_id, state

    def settle(
        self,
        usage_id: int,
        *,
        task: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[Decimal, BudgetState]:
        """Заменить резерв фактическим расходом."""
        cost = self.settings.pricing_for(task).cost(input_tokens, output_tokens)
        with self.lock:
            self.store.settle(
                usage_id,
                operation=task,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_rub=cost,
            )
            state = self.state()
        return cost, state

    def mark_failed(self, usage_id: int, task: str) -> None:
        """Запрос упал — резерв остаётся, меняется только пометка.

        Модель могла отработать, а ответ потеряться по сети; провайдер
        тарифицирует и такие попытки, включая повторы внутри SDK. Дешевле
        переоценить расход, чем незаметно выйти за лимит.
        """
        try:
            self.store.mark(usage_id, operation=f"{task}:failed")
        except Exception:  # noqa: BLE001 — учёт не должен маскировать исходную ошибку
            log.warning("резерв %s не помечен после ошибки запроса", usage_id)

    def check(self, *, task: str, input_tokens: int, max_output_tokens: int) -> BudgetState:
        """Зарезервировать худший допустимый расход логической проверкой."""
        with self.lock:
            state = self.state()
            reserved = self.settings.pricing_for(task).cost(input_tokens, max_output_tokens)
            if state.blocked or state.spent_rub + reserved > state.hard_limit_rub:
                raise AIBudgetExceeded(
                    "Технические данные: "
                    f"потрачено {state.spent_rub:.2f} ₽, "
                    f"запросу нужно до {reserved:.2f} ₽, "
                    f"лимит {state.hard_limit_rub:.2f} ₽"
                )
            return state

    def record(
        self,
        *,
        task: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> tuple[Decimal, BudgetState]:
        cost = self.settings.pricing_for(task).cost(input_tokens, output_tokens)
        with self.lock:
            self.store.record(
                operation=task,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_rub=cost,
            )
            state = self.state()
        return cost, state


def budget_guard_from_environment(
    values: Mapping[str, str] | None = None,
) -> AIBudgetGuard:
    """Собрать обязательный бюджетный guard с PostgreSQL-хранилищем."""
    from database import DatabaseSettings, connect

    environment = os.environ if values is None else values
    budget_settings = BudgetSettings.from_mapping(environment)
    database_settings = DatabaseSettings.from_mapping(environment)
    store = PostgresUsageStore(lambda: connect(database_settings))
    return AIBudgetGuard(budget_settings, store)
