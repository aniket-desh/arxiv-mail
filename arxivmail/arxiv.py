from __future__ import annotations

import html
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable

from .http import request_bytes
from .models import Paper


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
DC = "{http://purl.org/dc/elements/1.1/}"
RSS_BASE = "https://rss.arxiv.org/rss"
API_URL = "https://export.arxiv.org/api/query"
VERSION_RE = re.compile(r"^(?P<base>.+?)v(?P<version>\d+)$")
RSS_CATEGORIES_PER_FEED = 1


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return _clean(" ".join(self.parts))


def _clean(value: str | None) -> str:
    return " ".join(html.unescape(value or "").split())


def split_version(identifier: str) -> tuple[str, int]:
    normalized = identifier.removeprefix("oai:arXiv.org:").strip()
    match = VERSION_RE.match(normalized)
    if match:
        return match.group("base"), int(match.group("version"))
    return normalized, 1


def fetch_daily_rss(categories: Iterable[str]) -> list[Paper]:
    category_list = list(dict.fromkeys(categories))
    papers: list[Paper] = []
    for start in range(0, len(category_list), RSS_CATEGORIES_PER_FEED):
        if start:
            time.sleep(3.1)
        category_path = "+".join(category_list[start : start + RSS_CATEGORIES_PER_FEED])
        raw, _ = request_bytes(f"{RSS_BASE}/{category_path}", timeout=60)
        papers.extend(parse_rss(raw))
    return _merge_rss_papers(papers)


def parse_rss(raw: bytes) -> list[Paper]:
    root = ET.fromstring(raw)
    papers: dict[tuple[str, int], Paper] = {}
    for item in root.findall("./channel/item"):
        guid = _clean(item.findtext("guid"))
        if not guid:
            continue
        base_id, version = split_version(guid)
        arxiv_id = f"{base_id}v{version}"
        title = _clean(item.findtext("title"))
        creator = _clean(item.findtext(f"{DC}creator"))
        authors = tuple(part.strip() for part in re.split(r",|;|\band\b", creator) if part.strip())
        description = item.findtext("description") or ""
        extractor = _TextExtractor()
        extractor.feed(description)
        description_text = extractor.text()
        abstract = re.sub(r"^.*?Abstract:\s*", "", description_text, flags=re.IGNORECASE)
        categories = tuple(
            dict.fromkeys(_clean(node.text) for node in item.findall("category") if _clean(node.text))
        )
        primary = _clean(item.findtext(f"{ARXIV}primary_category"))
        if primary and primary not in categories:
            categories = (primary, *categories)
        published = parsedate_to_datetime(_clean(item.findtext("pubDate")))
        if published.tzinfo is None:
            published = published.astimezone()
        announce_type = _clean(item.findtext(f"{ARXIV}announce_type")) or "new"
        paper = Paper(
            arxiv_id=arxiv_id,
            base_id=base_id,
            version=version,
            title=title,
            authors=authors,
            abstract=abstract,
            categories=categories,
            primary_category=primary or (categories[0] if categories else ""),
            published=published,
            updated=published,
            comments=_clean(item.findtext(f"{ARXIV}comments")),
            journal_ref=_clean(item.findtext(f"{ARXIV}journal_reference")),
            announce_type=announce_type,
        )
        key = (base_id, version)
        previous = papers.get(key)
        if previous:
            merged_categories = tuple(dict.fromkeys((*previous.categories, *paper.categories)))
            precedence = {"new": 3, "replace": 2, "cross": 1, "replace-cross": 1}
            preferred = paper if precedence.get(paper.announce_type, 0) > precedence.get(previous.announce_type, 0) else previous
            paper = Paper(
                arxiv_id=preferred.arxiv_id,
                base_id=preferred.base_id,
                version=preferred.version,
                title=preferred.title,
                authors=preferred.authors,
                abstract=preferred.abstract,
                categories=merged_categories,
                primary_category=preferred.primary_category,
                published=preferred.published,
                updated=preferred.updated,
                comments=preferred.comments,
                journal_ref=preferred.journal_ref,
                announce_type=preferred.announce_type,
            )
        papers[key] = paper
    return sorted(papers.values(), key=lambda paper: paper.published, reverse=True)


def fetch_recent_api(
    categories: Iterable[str],
    since: datetime,
    until: datetime,
    *,
    max_results: int = 1000,
) -> list[Paper]:
    category_query = " OR ".join(f"cat:{category}" for category in dict.fromkeys(categories))
    date_range = f"submittedDate:[{since:%Y%m%d%H%M} TO {until:%Y%m%d%H%M}]"
    query = f"({category_query}) AND {date_range}"
    page_size = min(500, max_results)
    start = 0
    papers: list[Paper] = []
    total = max_results
    while start < min(total, max_results):
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": start,
                "max_results": min(page_size, max_results - start),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        raw, _ = request_bytes(f"{API_URL}?{params}", timeout=90)
        root = ET.fromstring(raw)
        total = int(root.findtext(f"{OPENSEARCH}totalResults") or 0)
        if total > max_results:
            raise RuntimeError(
                f"arXiv recovery matched {total} papers, exceeding the {max_results} result cap; "
                "narrow the interval or raise the cap before advancing delivery state"
            )
        page = parse_atom(raw, since=since, until=until)
        papers.extend(page)
        if not page or start + page_size >= min(total, max_results):
            break
        start += page_size
        time.sleep(3.1)
    return _latest_by_base(papers)


def fetch_by_ids(arxiv_ids: Iterable[str]) -> list[Paper]:
    identifiers = ",".join(dict.fromkeys(arxiv_ids))
    params = urllib.parse.urlencode({"id_list": identifiers, "max_results": len(identifiers.split(","))})
    raw, _ = request_bytes(f"{API_URL}?{params}", timeout=60)
    return parse_atom(raw)


def parse_atom(raw: bytes, *, since: datetime | None = None, until: datetime | None = None) -> list[Paper]:
    root = ET.fromstring(raw)
    papers: dict[str, Paper] = {}
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = _atom_identifier(entry)
        if not raw_id:
            continue
        if "/api/errors" in raw_id or raw_id.startswith("api/errors"):
            detail = _clean(entry.findtext(f"{ATOM}summary")) or _clean(entry.findtext(f"{ATOM}title"))
            raise RuntimeError(f"arXiv API error: {detail}")
        base_id, version = split_version(raw_id)
        published = datetime.fromisoformat(_clean(entry.findtext(f"{ATOM}published")).replace("Z", "+00:00"))
        updated = datetime.fromisoformat(_clean(entry.findtext(f"{ATOM}updated")).replace("Z", "+00:00"))
        if since and published < since:
            continue
        if until and published > until:
            continue
        categories = tuple(
            dict.fromkeys(node.attrib.get("term", "") for node in entry.findall(f"{ATOM}category") if node.attrib.get("term"))
        )
        primary_node = entry.find(f"{ARXIV}primary_category")
        primary = primary_node.attrib.get("term", "") if primary_node is not None else ""
        paper = Paper(
            arxiv_id=f"{base_id}v{version}",
            base_id=base_id,
            version=version,
            title=_clean(entry.findtext(f"{ATOM}title")),
            authors=tuple(
                _clean(author.findtext(f"{ATOM}name")) for author in entry.findall(f"{ATOM}author")
            ),
            abstract=_clean(entry.findtext(f"{ATOM}summary")),
            categories=categories,
            primary_category=primary or (categories[0] if categories else ""),
            published=published,
            updated=updated,
            comments=_clean(entry.findtext(f"{ARXIV}comment")),
            journal_ref=_clean(entry.findtext(f"{ARXIV}journal_ref")),
            announce_type="new" if version == 1 else "replace",
        )
        previous = papers.get(base_id)
        if previous is None or paper.version > previous.version:
            papers[base_id] = paper
    return sorted(papers.values(), key=lambda paper: paper.published, reverse=True)


def _atom_identifier(entry: ET.Element) -> str:
    candidates: list[str] = []
    for link in entry.findall(f"{ATOM}link"):
        if link.attrib.get("rel") == "alternate" and link.attrib.get("href"):
            candidates.append(link.attrib["href"])
    identifier = _clean(entry.findtext(f"{ATOM}id"))
    if identifier:
        candidates.append(identifier)
    for candidate in candidates:
        parsed = urllib.parse.urlparse(candidate)
        path = parsed.path or candidate
        normalized = re.sub(r"^/(?:abs|pdf|html)/", "", path).removesuffix(".pdf").strip("/")
        if normalized:
            return normalized
    return ""


def _latest_by_base(papers: Iterable[Paper]) -> list[Paper]:
    latest: dict[str, Paper] = {}
    for paper in papers:
        previous = latest.get(paper.base_id)
        if previous is None or paper.version > previous.version:
            latest[paper.base_id] = paper
    return sorted(latest.values(), key=lambda paper: paper.published, reverse=True)


def _merge_rss_papers(papers: Iterable[Paper]) -> list[Paper]:
    merged: dict[tuple[str, int], Paper] = {}
    precedence = {"new": 3, "replace": 2, "cross": 1, "replace-cross": 1}
    for paper in papers:
        key = (paper.base_id, paper.version)
        previous = merged.get(key)
        if previous is None:
            merged[key] = paper
            continue
        preferred = (
            paper
            if precedence.get(paper.announce_type, 0) > precedence.get(previous.announce_type, 0)
            else previous
        )
        supplement = previous if preferred is paper else paper
        merged[key] = Paper(
            arxiv_id=preferred.arxiv_id,
            base_id=preferred.base_id,
            version=preferred.version,
            title=preferred.title or supplement.title,
            authors=preferred.authors or supplement.authors,
            abstract=preferred.abstract or supplement.abstract,
            categories=tuple(dict.fromkeys((*preferred.categories, *supplement.categories))),
            primary_category=preferred.primary_category or supplement.primary_category,
            published=preferred.published,
            updated=preferred.updated,
            comments=preferred.comments or supplement.comments,
            journal_ref=preferred.journal_ref or supplement.journal_ref,
            announce_type=preferred.announce_type,
        )
    return sorted(merged.values(), key=lambda paper: paper.published, reverse=True)
