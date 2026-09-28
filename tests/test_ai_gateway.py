from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from ai_gateway import (  # noqa: E402
    AIGateway,
    AIConfigurationError,
    AIResponseError,
    GatewaySettings,
)


class FakeBudgetGuard:
    def __init__(self, spent="12.3400", warning=False):
        from decimal import Decimal

        self.spent = Decimal(spent)
        self.warning = warning
        self.checked = None
        self.recorded = None
        self.failed = None

    def reserve(self, **kwargs):
        self.checked = kwargs
        return 77, None

    def mark_failed(self, usage_id, task):
        self.failed = (usage_id, task)

    def settle(self, usage_id, **kwargs):
        from decimal import Decimal

        self.recorded = dict(kwargs, usage_id=usage_id)
        state = SimpleNamespace(
            spent_rub=self.spent,
            warning_rub=Decimal("350"),
            warning_reached=self.warning,
        )
        return Decimal("0.1061"), state


class FakeCompletions:
    def __init__(self, text: str | None = "готовый текст", usage=True):
        self.text = text
        self.usage = usage
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            model="resolved-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))],
            usage=(
                SimpleNamespace(prompt_tokens=123, completion_tokens=45)
                if self.usage
                else None
            ),
        )


class FakeClient:
    def __init__(self, text: str | None = "готовый текст", usage=True):
        self.completions = FakeCompletions(text, usage=usage)
        self.chat = SimpleNamespace(completions=self.completions)
        self.models = SimpleNamespace(
            list=lambda: SimpleNamespace(
                data=[SimpleNamespace(id="openai/z-model"), SimpleNamespace(id="openai/a-model")]
            )
        )


class GatewaySettingsTest(unittest.TestCase):
    def test_requires_key_and_both_models(self):
        with self.assertRaisesRegex(AIConfigurationError, "ключ AI Gateway"):
            GatewaySettings.from_mapping({})

    def test_rejects_non_https_gateway(self):
        with self.assertRaisesRegex(AIConfigurationError, "https"):
            GatewaySettings.from_mapping(
                {
                    "TIMEWEB_AI_API_KEY": "secret",
                    "AI_MODEL_FAST": "fast",
                    "AI_MODEL_WRITER": "writer",
                    "TIMEWEB_AI_BASE_URL": "http://example.test/v1",
                }
            )


class AIGatewayTest(unittest.TestCase):
    def setUp(self):
        self.settings = GatewaySettings.from_mapping(
            {
                "TIMEWEB_AI_API_KEY": "secret",
                "AI_MODEL_FAST": "fast-model",
                "AI_MODEL_WRITER": "writer-model",
            }
        )

    def test_routes_writer_task_and_reports_usage(self):
        client = FakeClient()
        budget = FakeBudgetGuard()
        result = AIGateway(self.settings, client=client, budget_guard=budget).generate(
            task="writer",
            instructions="правила",
            content="источники",
        )

        self.assertEqual(result.text, "готовый текст")
        self.assertEqual(result.model, "resolved-model")
        self.assertEqual(result.input_tokens, 123)
        self.assertEqual(result.output_tokens, 45)
        self.assertEqual(str(result.estimated_cost_rub), "0.1061")
        self.assertEqual(str(result.monthly_spend_rub), "12.3400")
        self.assertFalse(result.budget_warning)
        self.assertEqual(client.completions.kwargs["model"], "writer-model")
        self.assertEqual(client.completions.kwargs["max_completion_tokens"], 4000)
        self.assertEqual(client.completions.kwargs["messages"][0]["role"], "system")
        self.assertEqual(budget.checked["max_output_tokens"], 4000)
        self.assertEqual(budget.recorded["input_tokens"], 123)

    def test_rejects_empty_response(self):
        with self.assertRaises(AIResponseError):
            AIGateway(
                self.settings,
                client=FakeClient(" "),
                budget_guard=FakeBudgetGuard(),
            ).generate(
                task="fast",
                instructions="правила",
                content="источники",
            )

    def test_refuses_generation_without_budget_guard(self):
        with self.assertRaisesRegex(AIConfigurationError, "AIBudgetGuard"):
            AIGateway(self.settings, client=FakeClient()).generate(
                task="fast",
                instructions="правила",
                content="источники",
            )

    def test_uses_reserved_maximum_when_provider_omits_usage(self):
        budget = FakeBudgetGuard()
        AIGateway(
            self.settings,
            client=FakeClient(usage=False),
            budget_guard=budget,
        ).generate(
            task="fast",
            instructions="abc",
            content="def",
            max_tokens=200,
        )

        self.assertEqual(budget.recorded["input_tokens"], 70)
        self.assertEqual(budget.recorded["output_tokens"], 200)

    def test_lists_real_model_ids(self):
        models = AIGateway(self.settings, client=FakeClient()).list_models()
        self.assertEqual(models, ["openai/a-model", "openai/z-model"])

    def test_failed_request_keeps_the_reservation(self):
        # Модель могла отработать до обрыва, и провайдер это тарифицирует.
        # Снимать резерв нельзя: так лимит обходится незаметно.
        class BrokenCompletions:
            def create(self, **kwargs):
                raise RuntimeError("сеть отвалилась")

        client = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))
        budget = FakeBudgetGuard()
        with self.assertRaises(RuntimeError):
            AIGateway(self.settings, client=client, budget_guard=budget).generate(
                task="fast", instructions="правила", content="источники"
            )
        self.assertEqual(budget.failed, (77, "fast"))


if __name__ == "__main__":
    unittest.main()
