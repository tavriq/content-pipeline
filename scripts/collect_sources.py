#!/usr/bin/env python3
"""Сбор свежих публикаций из проверяемых источников.

Первый адаптер использует официальный PubMed E-utilities API. Он делает три
запроса на один запуск (ESearch + ESummary + EFetch), не скачивает полные
тексты и исключает препринты как на уровне запроса, так и после получения данных.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "sources.json"
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RETRIES = 3
RETRY_PAUSE_SECONDS = 2.0

log = logging.getLogger(__name__)
PREPRINT_MARKERS = frozenset({"preprint"})
MAX_ABSTRACT_CHARS = 4000


class SourceUnavailable(RuntimeError):
    """Источник временно недоступен или ответил ошибкой."""


class SourceConfigurationError(ValueError):
    """Настройки источника отсутствуют или некорректны."""


@dataclass(frozen=True)
class SourceItem:
    source: str
    external_id: str
    url: str
    title: str
    summary: str
    language: str
    published_at: str | None
    content_hash: str
    is_preprint: bool
    raw_payload: dict[str, Any]


def read_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SourceConfigurationError("config/sources.json должен содержать объект")
    return data


def _request(endpoint: str, params: Mapping[str, str], timeout: float, accept: str) -> bytes:
    """Запрос к E-utilities с повтором: сеть и NCBI отвечают не всегда.

    Повторов ровно два, с нарастающей паузой. Больше не нужно: конвейер
    ходит раз в сутки, а без ключа NCBI разрешает три запроса в секунду.
    """
    url = f"{EUTILS_BASE}/{endpoint}?{urlencode(params)}"
    request = Request(
        url,
        headers={"Accept": accept, "User-Agent": "blog-content-pipeline/1.0"},
    )
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 — фиксированный HTTPS host
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            last = error
            log.warning("%s не ответил (попытка %s): %s", endpoint, attempt + 1, error)
            if attempt + 1 < RETRIES:
                sleep(RETRY_PAUSE_SECONDS * (attempt + 1))
    raise SourceUnavailable(f"PubMed не ответил после {RETRIES} попыток: {last}")


def default_transport(endpoint: str, params: Mapping[str, str], timeout: float) -> dict[str, Any]:
    payload = _request(endpoint, params, timeout, "application/json")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceUnavailable(f"PubMed вернул не JSON: {error}") from error


def default_xml_transport(endpoint: str, params: Mapping[str, str], timeout: float) -> bytes:
    return _request(endpoint, params, timeout, "application/xml")


def parse_pubmed_abstracts(payload: bytes | str) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise SourceConfigurationError("PubMed EFetch вернул некорректный XML") from error

    abstracts: dict[str, str] = {}
    for article in root.findall(".//PubmedArticle"):
        identifier = article.findtext("./MedlineCitation/PMID", default="").strip()
        if not identifier:
            continue
        parts: list[str] = []
        for node in article.findall("./MedlineCitation/Article/Abstract/AbstractText"):
            text = " ".join("".join(node.itertext()).split())
            if not text:
                continue
            label = " ".join(node.attrib.get("Label", "").split())
            parts.append(f"{label}: {text}" if label else text)
        abstract = " ".join(parts)
        if abstract:
            abstracts[identifier] = abstract[:MAX_ABSTRACT_CHARS]
    return abstracts


def parse_pubmed_date(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y %b %d", "%Y %b", "%Y"):
        try:
            parsed = datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def content_hash(title: str, url: str) -> str:
    canonical = " ".join(title.casefold().split()) + "\n" + url.strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PubMedSource:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        email: str,
        api_key: str | None = None,
        transport: Callable[[str, Mapping[str, str], float], dict[str, Any]] = default_transport,
        abstract_transport: Callable[
            [str, Mapping[str, str], float], bytes | str
        ] = default_xml_transport,
        timeout: float = 30.0,
    ):
        self.query = str(config.get("query", "")).strip()
        self.days = int(config.get("days", 7))
        self.limit = int(config.get("limit", 50))
        # Ежедневный сбор отвечает на «что вышло на этой неделе» и сортирует
        # по дате. Поиск под конкретную тему — на «что об этом известно», и
        # там дата не значит ничего: нужен лучший ответ, а не последний.
        self.sort = str(config.get("sort", "date_desc")).strip() or "date_desc"
        self.email = email.strip()
        self.api_key = (api_key or "").strip()
        self.transport = transport
        self.abstract_transport = abstract_transport
        self.timeout = timeout
        if not self.query:
            raise SourceConfigurationError("Для PubMed не задан query")
        if not self.email or "@" not in self.email:
            raise SourceConfigurationError("Для PubMed нужен NCBI_EMAIL")
        if self.days <= 0 or not 1 <= self.limit <= 200:
            raise SourceConfigurationError("PubMed days должен быть > 0, limit — от 1 до 200")

    def _common_params(self) -> dict[str, str]:
        params = {
            "db": "pubmed",
            "retmode": "json",
            "tool": "blog_content_pipeline",
            "email": self.email,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def fetch(self) -> list[SourceItem]:
        search_params = self._common_params()
        search_params.update(
            {
                "term": self.query,
                "reldate": str(self.days),
                "datetype": "pdat",
                "sort": self.sort,
                "retmax": str(self.limit),
            }
        )
        search = self.transport("esearch.fcgi", search_params, self.timeout)
        # NCBI отвечает двумястами и кладёт ошибку внутрь JSON. Без этой
        # проверки сломанный запрос выглядел как «свежих публикаций нет»:
        # конвейер бодро рапортовал ноль, и никто ничего не замечал.
        search_result = search.get("esearchresult", {})
        error = search_result.get("ERROR") or search_result.get("error")
        if error:
            raise SourceUnavailable(f"PubMed вернул ошибку: {error}")
        identifiers = [str(value) for value in search_result.get("idlist", [])]
        if not identifiers:
            return []

        summary_params = self._common_params()
        summary_params.update({"id": ",".join(identifiers), "version": "2.0"})
        summary = self.transport("esummary.fcgi", summary_params, self.timeout)
        result = summary.get("result", {})

        fetch_params = self._common_params()
        fetch_params.update({"id": ",".join(identifiers), "retmode": "xml"})
        abstracts = parse_pubmed_abstracts(
            self.abstract_transport("efetch.fcgi", fetch_params, self.timeout)
        )

        items: list[SourceItem] = []
        for identifier in identifiers:
            record = result.get(identifier)
            if not isinstance(record, dict):
                continue
            publication_types = {
                str(value).casefold() for value in record.get("pubtype", []) if value
            }
            is_preprint = bool(publication_types & PREPRINT_MARKERS)
            if is_preprint:
                continue
            title = " ".join(str(record.get("title", "")).split())
            if not title:
                continue
            url = f"https://pubmed.ncbi.nlm.nih.gov/{identifier}/"
            published_at = parse_pubmed_date(record.get("sortpubdate") or record.get("pubdate"))
            journal = str(record.get("fulljournalname") or record.get("source") or "PubMed")
            abstract = abstracts.get(identifier, "")
            items.append(
                SourceItem(
                    source="PubMed",
                    external_id=identifier,
                    url=url,
                    title=title,
                    summary=abstract or journal,
                    language="en",
                    published_at=published_at,
                    content_hash=content_hash(title, url),
                    is_preprint=False,
                    raw_payload={
                        "publication_types": sorted(publication_types),
                        "journal": journal,
                        "abstract_available": bool(abstract),
                        "authors": record.get("authors", []),
                        "article_ids": record.get("articleids", []),
                    },
                )
            )
        return items


def search_evidence(
    query: str,
    *,
    email: str,
    api_key: str | None = None,
    limit: int = 4,
    years: int = 8,
    transport: Callable[[str, Mapping[str, str], float], dict[str, Any]] = default_transport,
    abstract_transport: Callable[
        [str, Mapping[str, str], float], bytes | str
    ] = default_xml_transport,
    timeout: float = 30.0,
) -> list[SourceItem]:
    """Найти доказательную базу под заданную тему.

    Ежедневный сбор отвечает на вопрос «что вышло на этой неделе». Здесь
    вопрос обратный: тема уже выбрана человеком, и под неё нужны источники —
    не обязательно свежие, но обязательно весомые. Поэтому окно измеряется
    годами, а не днями, и остаются только рекомендации, обзоры и метаанализы.
    """
    term = query.strip()
    if not term:
        raise SourceConfigurationError("Пустой поисковый запрос")
    # Общий Review из фильтра исключён намеренно: по запросу про витамин D он
    # приносил обзоры про ХОБЛ и кардиоонкологию, где термин просто упомянут.
    # Остаются рекомендации, метаанализы и систематические обзоры — то, что
    # стоит наверху иерархии источников в системном промпте.
    filtered = (
        f"({term}) AND (Guideline[Publication Type] OR Practice Guideline[Publication Type] "
        "OR Meta-Analysis[Publication Type] OR Systematic Review[Publication Type]) "
        "NOT Preprint[Publication Type]"
    )
    source = PubMedSource(
        {
            "query": filtered,
            "days": max(1, years) * 365,
            "limit": max(1, min(limit, 10)),
            "sort": "relevance",
        },
        email=email,
        api_key=api_key,
        transport=transport,
        abstract_transport=abstract_transport,
        timeout=timeout,
    )
    return source.fetch()


def search_evidence_from_environment(query: str, limit: int = 4) -> list[SourceItem]:
    email = os.environ.get("NCBI_EMAIL", "")
    if not email:
        raise SourceConfigurationError("Для PubMed нужен NCBI_EMAIL")
    return search_evidence(
        query,
        email=email,
        api_key=os.environ.get("NCBI_API_KEY"),
        limit=limit,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Записать JSON; иначе только статистика")
    parser.add_argument(
        "--save",
        action="store_true",
        help="Сохранить новые публикации в PostgreSQL с дедупликацией",
    )
    args = parser.parse_args()

    config = read_config().get("pubmed", {})
    if not config.get("enabled", False):
        print("PubMed отключён")
        return 0
    source = PubMedSource(
        config,
        email=os.environ.get("NCBI_EMAIL", ""),
        api_key=os.environ.get("NCBI_API_KEY"),
    )
    items = source.fetch()
    print(f"PubMed: получено {len(items)}, препринты исключены")
    if args.output:
        args.output.write_text(
            json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Результат: {args.output}")
    if args.save:
        from database import DatabaseSettings, connect
        from source_store import PUBMED_DEFINITION, SourceStore

        settings = DatabaseSettings.from_environment()
        with connect(settings) as connection:
            saved = SourceStore(connection).save_items(items, PUBMED_DEFINITION)
        print(
            "PostgreSQL: "
            f"добавлено {saved.inserted}, дублей {saved.duplicates}, "
            f"препринтов отклонено {saved.preprints_excluded}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
