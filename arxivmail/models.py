from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Paper:
    arxiv_id: str
    base_id: str
    version: int
    title: str
    authors: tuple[str, ...]
    abstract: str
    categories: tuple[str, ...]
    primary_category: str
    published: datetime
    updated: datetime
    comments: str = ""
    journal_ref: str = ""
    announce_type: str = "new"

    @property
    def abstract_url(self) -> str:
        return f"https://arxiv.org/abs/{self.arxiv_id}"

    @property
    def html_url(self) -> str:
        return f"https://arxiv.org/html/{self.arxiv_id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.arxiv_id}"

    @property
    def announcement_date(self) -> date:
        return self.published.date()

    def prompt_record(self) -> dict[str, Any]:
        return {
            "id": self.arxiv_id,
            "title": self.title,
            "authors": list(self.authors),
            "categories": list(self.categories),
            "published": self.published.date().isoformat(),
            "abstract": self.abstract,
            "comments": self.comments,
        }


@dataclass(frozen=True, slots=True)
class ZoteroItem:
    key: str
    title: str
    abstract: str
    tags: tuple[str, ...]
    item_type: str
    date_modified: str
    creators: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    collection_keys: tuple[str, ...] = ()

    @property
    def embedding_text(self) -> str:
        tags = ", ".join(self.tags)
        collections = ", ".join(self.collections)
        return f"{self.title}\n{self.abstract}\nTags: {tags}\nCollections: {collections}".strip()


@dataclass(frozen=True, slots=True)
class LocalScore:
    lexical: float = 0.0
    semantic: float = 0.0
    zotero: float = 0.0
    combined: float = 0.0
    matched_terms: tuple[str, ...] = ()
    matched_clusters: tuple[str, ...] = ()
    focus_hits: tuple[str, ...] = ()
    nearest_zotero: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Assessment:
    arxiv_id: str
    tier: str
    score: int
    confidence: float
    fit_type: str
    matched_clusters: tuple[str, ...]
    monthly_focus_hits: tuple[str, ...]
    why_match: str
    summary_sentences: tuple[str, ...]
    reading_value: str

    def __post_init__(self) -> None:
        if self.tier not in {"M", "P", "N"}:
            raise ValueError(f"Invalid assessment tier: {self.tier}")
        if not 0 <= self.score <= 100:
            raise ValueError("Assessment score must be between 0 and 100")
        if len(self.summary_sentences) not in {3, 4}:
            raise ValueError("A paper summary must contain 3 or 4 sentences")


@dataclass(frozen=True, slots=True)
class RankedPaper:
    paper: Paper
    assessment: Assessment
    local_score: LocalScore = field(default_factory=LocalScore)
    is_revision: bool = False


@dataclass(frozen=True, slots=True)
class Digest:
    digest_date: date
    run_id: str
    monthly_focus: str
    screened_count: int
    main: tuple[RankedPaper, ...]
    less_urgent: tuple[RankedPaper, ...]
    revisions: tuple[RankedPaper, ...] = ()
    notice: str = ""
    model: str = "gpt-5.6-luna"

    @property
    def selected_count(self) -> int:
        return len(self.main) + len(self.less_urgent) + len(self.revisions)

    @property
    def all_ranked(self) -> tuple[RankedPaper, ...]:
        return self.main + self.less_urgent + self.revisions
