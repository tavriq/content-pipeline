from __future__ import annotations

import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from ai_budget import (  # noqa: E402
    AIBudgetConfigurationError,
    AIBudgetExceeded,
    AIBudgetGuard,
    BudgetSettings,
    TokenPricing,
    month_bounds,
)


class FakeUsageStore:
    def __init__(self, spent: str = "0"):
        self.spent = Decimal(spent)
        self.records = []
        self.bounds = None

    def monthly_spend(self, month_start, next_month_start):
        self.bounds = (month_start, next_month_start)
        return self.spent

    def record(self, **kwargs):
        self.records.append(kwargs)
        self.spent += kwargs["estimated_cost_rub"]
        return len(self.records)

    def settle(self, usage_id, **kwargs):
        previous = self.records[usage_id - 1]
        self.spent += kwargs["estimated_cost_rub"] - previous["estimated_cost_rub"]
        self.records[usage_id - 1] = dict(kwargs, usage_id=usage_id)

    def mark(self, usage_id, *, operation):
        self.records[usage_id - 1]["operation"] = operation


class BudgetSettingsTest(unittest.TestCase):
    def test_defaults_match_approved_timeweb_limits_and_prices(self):
        settings = BudgetSettings.from_mapping({})
        self.assertEqual(settings.warning_rub, Decimal("350"))
        self.assertEqual(settings.hard_limit_rub, Decimal("500"))
        self.assertEqual(settings.fast_pricing.input_rub_per_million, Decimal("27"))
        self.assertEqual(settings.writer_pricing.output_rub_per_million, Decimal("1620"))

    def test_warning_must_be_below_hard_limit(self):
        with self.assertRaises(AIBudgetConfigurationError):
            BudgetSettings.from_mapping(
                {"AI_BUDGET_WARNING_RUB": "500", "AI_BUDGET_HARD_LIMIT_RUB": "500"}
            )


class TokenPricingTest(unittest.TestCase):
    def test_calculates_cost_to_four_decimal_places(self):
        pricing = TokenPricing(Decimal("270"), Decimal("1620"))
        self.assertEqual(pricing.cost(123, 45), Decimal("0.1061"))


class AIBudgetGuardTest(unittest.TestCase):
    def setUp(self):
        self.settings = BudgetSettings.from_mapping({})

    def test_blocks_request_that_could_cross_hard_limit(self):
        store = FakeUsageStore("499")
        guard = AIBudgetGuard(self.settings, store)

        with self.assertRaises(AIBudgetExceeded) as caught:
            guard.check(task="writer", input_tokens=1000, max_output_tokens=4000)

        self.assertIn("Напиши Сане", str(caught.exception))
        self.assertIn("проверит баланс", str(caught.exception))
        self.assertNotIn("500.00", str(caught.exception))
        self.assertIn("500.00", caught.exception.technical_details)

    def test_records_actual_cost_and_raises_warning_state(self):
        store = FakeUsageStore("349.95")
        guard = AIBudgetGuard(self.settings, store)

        cost, state = guard.record(
            task="writer",
            model="openai/gpt-5.6-terra",
            input_tokens=1000,
            output_tokens=100,
        )

        self.assertEqual(cost, Decimal("0.4320"))
        self.assertEqual(state.spent_rub, Decimal("350.3820"))
        self.assertTrue(state.warning_reached)
        self.assertEqual(store.records[0]["operation"], "writer")

    def test_calendar_month_uses_moscow_timezone(self):
        timezone = ZoneInfo("Europe/Moscow")
        start, next_start = month_bounds(datetime(2026, 12, 15, tzinfo=timezone))
        self.assertEqual(start, datetime(2026, 12, 1, tzinfo=timezone))
        self.assertEqual(next_start, datetime(2027, 1, 1, tzinfo=timezone))

class BudgetReservationTest(unittest.TestCase):
    def test_reservation_holds_the_limit_until_the_request_finishes(self):
        # Два параллельных запроса раньше видели одну и ту же сумму: между
        # проверкой и записью факта проходил весь вызов к модели.
        store = FakeUsageStore("495")
        guard = AIBudgetGuard(BudgetSettings.from_mapping({}), store)

        usage_id, _ = guard.reserve(task="fast", input_tokens=10_000, max_output_tokens=6_000)
        self.assertEqual(store.records[0]["operation"], "fast:reserved")

        with self.assertRaises(AIBudgetExceeded):
            guard.reserve(task="writer", input_tokens=10_000, max_output_tokens=6_000)

        cost, state = guard.settle(
            usage_id, task="fast", model="m", input_tokens=100, output_tokens=200
        )
        self.assertEqual(store.records[0]["operation"], "fast")
        self.assertLess(cost, Decimal("1"))
        self.assertLess(state.spent_rub, Decimal("496"))


if __name__ == "__main__":
    unittest.main()
