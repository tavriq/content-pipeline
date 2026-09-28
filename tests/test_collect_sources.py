from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import collect_sources  # noqa: E402
from collect_sources import (  # noqa: E402
    PubMedSource,
    SourceConfigurationError,
    content_hash,
    parse_pubmed_abstracts,
    parse_pubmed_date,
)


class FakePubMedTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, endpoint, params, timeout):
        self.calls.append((endpoint, dict(params), timeout))
        if endpoint == "esearch.fcgi":
            return {"esearchresult": {"idlist": ["101", "102", "103"]}}
        return {
            "result": {
                "101": {
                    "title": "  Thyroid   guideline update ",
                    "sortpubdate": "2026/08/20 00:00",
                    "fulljournalname": "Clinical Endocrinology",
                    "pubtype": ["Guideline"],
                    "authors": [],
                    "articleids": [],
                },
                "102": {
                    "title": "A preprint that must not pass",
                    "pubdate": "2026 Aug 19",
                    "pubtype": ["Preprint"],
                },
                "103": {"title": "", "pubtype": ["Journal Article"]},
            }
        }


class FakeAbstractTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, endpoint, params, timeout):
        self.calls.append((endpoint, dict(params), timeout))
        return b"""
        <PubmedArticleSet>
          <PubmedArticle>
            <MedlineCitation>
              <PMID>101</PMID>
              <Article>
                <Abstract>
                  <AbstractText Label="BACKGROUND">Why this matters.</AbstractText>
                  <AbstractText Label="RESULTS">New evidence.</AbstractText>
                </Abstract>
              </Article>
            </MedlineCitation>
          </PubmedArticle>
        </PubmedArticleSet>
        """


class PubMedSourceTest(unittest.TestCase):
    def test_requires_contact_email(self):
        with self.assertRaisesRegex(SourceConfigurationError, "NCBI_EMAIL"):
            PubMedSource({"query": "thyroid"}, email="")

    def test_fetches_in_two_calls_and_excludes_preprints(self):
        transport = FakePubMedTransport()
        abstract_transport = FakeAbstractTransport()
        source = PubMedSource(
            {"query": "thyroid NOT Preprint[Publication Type]", "days": 7, "limit": 20},
            email="dev@example.com",
            transport=transport,
            abstract_transport=abstract_transport,
        )
        items = source.fetch()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "101")
        self.assertEqual(items[0].title, "Thyroid guideline update")
        self.assertFalse(items[0].is_preprint)
        self.assertEqual(
            items[0].summary,
            "BACKGROUND: Why this matters. RESULTS: New evidence.",
        )
        self.assertTrue(items[0].raw_payload["abstract_available"])
        self.assertEqual([call[0] for call in transport.calls], ["esearch.fcgi", "esummary.fcgi"])
        self.assertEqual([call[0] for call in abstract_transport.calls], ["efetch.fcgi"])
        self.assertEqual(transport.calls[0][1]["reldate"], "7")
        self.assertEqual(transport.calls[0][1]["sort"], "date_desc")

    def test_stable_content_hash(self):
        url = "https://pubmed.ncbi.nlm.nih.gov/101/"
        self.assertEqual(
            content_hash("Thyroid  guideline", url),
            content_hash(" thyroid guideline ", url),
        )

    def test_parses_supported_dates_and_rejects_unknown(self):
        self.assertEqual(parse_pubmed_date("2026 Aug 20"), "2026-08-20T00:00:00+00:00")
        self.assertIsNone(parse_pubmed_date("Autumn 2026"))

    def test_rejects_invalid_efetch_xml(self):
        with self.assertRaisesRegex(SourceConfigurationError, "некорректный XML"):
            parse_pubmed_abstracts("<broken>")


class EvidenceSearchTest(unittest.TestCase):
    """Поиск под тему — не то же самое, что ежедневный сбор."""

    def _capture(self):
        captured = {}

        def transport(endpoint, params, timeout):
            captured[endpoint] = dict(params)
            if endpoint == "esearch.fcgi":
                return {"esearchresult": {"idlist": []}}
            return {"result": {}}

        return captured, transport

    def test_sorts_by_relevance_not_by_date(self):
        # Дата тут не значит ничего: нужен лучший ответ на вопрос, а не
        # последняя вышедшая статья.
        captured, transport = self._capture()
        collect_sources.search_evidence(
            "vitamin D", email="blog@example.test", transport=transport
        )
        self.assertEqual(captured["esearch.fcgi"]["sort"], "relevance")

    def test_keeps_only_the_top_of_the_evidence_hierarchy(self):
        # Общий Review приносил обзоры про ХОБЛ и кардиоонкологию, где
        # витамин D просто упомянут.
        captured, transport = self._capture()
        collect_sources.search_evidence(
            "vitamin D", email="blog@example.test", transport=transport
        )
        term = captured["esearch.fcgi"]["term"]
        self.assertIn("Guideline[Publication Type]", term)
        self.assertIn("Meta-Analysis[Publication Type]", term)
        self.assertNotIn("OR Review[Publication Type]", term)
        self.assertIn("NOT Preprint[Publication Type]", term)

    def test_daily_collection_still_sorts_by_date(self):
        captured, transport = self._capture()
        collect_sources.PubMedSource(
            {"query": "thyroid", "days": 7, "limit": 10},
            email="blog@example.test",
            transport=transport,
        ).fetch()
        self.assertEqual(captured["esearch.fcgi"]["sort"], "date_desc")

    def test_window_is_measured_in_years(self):
        captured, transport = self._capture()
        collect_sources.search_evidence(
            "vitamin D", email="blog@example.test", transport=transport, years=8
        )
        self.assertEqual(captured["esearch.fcgi"]["reldate"], str(8 * 365))


if __name__ == "__main__":
    unittest.main()
