#!/usr/bin/env python3
"""Доступ к LLM через OpenAI-совместимый AI Gateway Timeweb.

Модели задаются конфигурацией: их точные идентификаторы берутся из панели
Timeweb или ``GET /models``. Так смена модели не требует правки кода.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from ai_budget import AIBudgetGuard, budget_guard_from_environment

DEFAULT_BASE_URL = "https://api.timeweb.ai/v1"
log = logging.getLogger(__name__)


class AIConfigurationError(ValueError):
    """Обязательная настройка AI Gateway отсутствует или некорректна."""


class AIResponseError(RuntimeError):
    """Провайдер вернул ответ без текста."""


@dataclass(frozen=True)
class GatewaySettings:
    api_key: str
    fast_model: str
    writer_model: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 120.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "GatewaySettings":
        required = {
            "TIMEWEB_AI_API_KEY": "ключ AI Gateway",
            "AI_MODEL_FAST": "модель обработки новостей",
            "AI_MODEL_WRITER": "модель подготовки публикаций",
        }
        missing = [description for key, description in required.items() if not values.get(key, "").strip()]
        if missing:
            raise AIConfigurationError("Не настроено: " + ", ".join(missing))

        base_url = values.get("TIMEWEB_AI_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise AIConfigurationError("TIMEWEB_AI_BASE_URL должен начинаться с https://")

        raw_timeout = values.get("AI_TIMEOUT_SECONDS", "120").strip()
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise AIConfigurationError("AI_TIMEOUT_SECONDS должен быть числом") from error
        if timeout <= 0:
            raise AIConfigurationError("AI_TIMEOUT_SECONDS должен быть больше нуля")

        return cls(
            api_key=values["TIMEWEB_AI_API_KEY"].strip(),
            fast_model=values["AI_MODEL_FAST"].strip(),
            writer_model=values["AI_MODEL_WRITER"].strip(),
            base_url=base_url,
            timeout_seconds=timeout,
        )

    @classmethod
    def from_environment(cls) -> "GatewaySettings":
        return cls.from_mapping(os.environ)


@dataclass(frozen=True)
class AIResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_rub: Decimal | None = None
    monthly_spend_rub: Decimal | None = None
    budget_warning: bool = False


class AIGateway:
    """Минимальный клиент Gateway без хранения истории у провайдера."""

    def __init__(
        self,
        settings: GatewaySettings,
        client: Any | None = None,
        budget_guard: AIBudgetGuard | None = None,
    ):
        self.settings = settings
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError("Установите зависимости: pip install -r requirements.txt") from error
            client = OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                max_retries=2,
            )
        self.client = client
        self.budget_guard = budget_guard

    @classmethod
    def from_environment(cls, client: Any | None = None) -> "AIGateway":
        """Безопасная сборка: генерация сразу подключена к месячному бюджету."""
        return cls(
            GatewaySettings.from_environment(),
            client=client,
            budget_guard=budget_guard_from_environment(),
        )

    def generate(
        self,
        *,
        task: str,
        instructions: str,
        content: str,
        max_tokens: int = 4000,
    ) -> AIResult:
        """Сгенерировать текст моделью для ``fast`` или ``writer`` задачи."""
        models = {
            "fast": self.settings.fast_model,
            "writer": self.settings.writer_model,
        }
        if task not in models:
            raise ValueError("task должен быть 'fast' или 'writer'")

        model = models[task]
        # Для русского текста один символ на токен — заведомо осторожная
        # оценка. Фактические значения после ответа берём у провайдера.
        estimated_input_tokens = len(instructions) + len(content) + 64
        if self.budget_guard is None:
            raise AIConfigurationError(
                "AI-генерация запрещена без AIBudgetGuard: лимит расходов нельзя обойти"
            )
        # Резерв пишется в базу до запроса и заменяется фактом после.
        # Проверка без записи лимит не держала: параллельные процессы
        # видели одну и ту же сумму, пока шёл вызов на несколько минут.
        reservation_id, _ = self.budget_guard.reserve(
            task=task,
            input_tokens=estimated_input_tokens,
            max_output_tokens=max_tokens,
        )
        try:
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
                # У GPT-5 max_tokens устарел; этот параметр учитывает также
                # reasoning-токены и ограничивает неожиданный расход средств.
                max_completion_tokens=max_tokens,
            )
        except Exception:
            self.budget_guard.mark_failed(reservation_id, task)
            raise
        usage = getattr(response, "usage", None)
        # Если провайдер временно не вернул usage, нулевой расход записывать
        # опасно: бюджет можно незаметно обойти. Берём зарезервированный максимум.
        input_tokens = int(
            getattr(usage, "prompt_tokens", 0) or estimated_input_tokens
        )
        output_tokens = int(getattr(usage, "completion_tokens", 0) or max_tokens)
        resolved_model = getattr(response, "model", None) or model
        cost, budget_state = self.budget_guard.settle(
            reservation_id,
            task=task,
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        if budget_state.warning_reached:
            log.warning(
                "Расход AI за месяц %.2f ₽: порог предупреждения %.2f ₽",
                budget_state.spent_rub,
                budget_state.warning_rub,
            )

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise AIResponseError(f"Модель {model} не вернула ни одного варианта")
        text = choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise AIResponseError(f"Модель {model} вернула пустой ответ")

        return AIResult(
            text=text.strip(),
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_rub=cost,
            monthly_spend_rub=budget_state.spent_rub,
            budget_warning=budget_state.warning_reached,
        )

    def list_models(self) -> list[str]:
        """Получить реальные model ID, доступные текущему ключу Timeweb."""
        return sorted(item.id for item in self.client.models.list().data)
