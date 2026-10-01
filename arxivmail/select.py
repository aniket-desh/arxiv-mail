from __future__ import annotations

from collections import Counter
from typing import Iterable

from .config import AppConfig
from .models import Assessment, LocalScore, Paper, RankedPaper


def select_digest_papers(
    assessed: Iterable[tuple[Paper, LocalScore, Assessment]],
    revision_ids: set[str],
    config: AppConfig,
) -> tuple[tuple[RankedPaper, ...], tuple[RankedPaper, ...], tuple[RankedPaper, ...]]:
    maximum = int(config.delivery.get("max_total", 15))
    max_less = int(config.delivery.get("max_less_urgent", 3))
    confidence_min = float(config.selection.get("minimum_confidence", 0.45))
    must_score = _score_threshold(config.selection.get("must_read_min_score", 0.78))
    less_score = _score_threshold(config.selection.get("less_urgent_min_score", 0.56))
    revision_score = _score_threshold(config.selection.get("material_revision_min_score", 0.72))

    ranked = [
        RankedPaper(
            paper=paper,
            assessment=assessment,
            local_score=local,
            is_revision=paper.base_id in revision_ids,
        )
        for paper, local, assessment in assessed
        if assessment.confidence >= confidence_min
    ]
    revisions = [
        item
        for item in ranked
        if item.is_revision and item.assessment.tier == "M" and item.assessment.score >= revision_score
    ]
    strong = [
        item
        for item in ranked
        if not item.is_revision and item.assessment.tier == "M" and item.assessment.score >= must_score
    ]
    possible = [
        item
        for item in ranked
        if not item.is_revision and item.assessment.tier == "P" and item.assessment.score >= less_score
    ]
    key = lambda item: (item.assessment.score, item.local_score.combined)
    revisions.sort(key=key, reverse=True)
    strong.sort(key=key, reverse=True)
    possible.sort(key=key, reverse=True)

    chosen_revisions = tuple(revisions[: min(3, maximum)])
    remaining = maximum - len(chosen_revisions)
    chosen_strong = tuple(_diverse_top(strong, remaining, config))
    remaining = maximum - len(chosen_revisions) - len(chosen_strong)
    chosen_less = tuple(possible[: min(max_less, remaining)])
    return chosen_strong, chosen_less, chosen_revisions


def _diverse_top(items: list[RankedPaper], limit: int, config: AppConfig) -> list[RankedPaper]:
    if len(items) <= limit:
        return items
    soft_cap = int(config.selection.get("max_per_cluster_before_diversity_penalty", 5))
    selected: list[RankedPaper] = []
    deferred: list[RankedPaper] = []
    counts: Counter[str] = Counter()
    for item in items:
        clusters = item.assessment.matched_clusters or item.local_score.matched_clusters or ("other",)
        primary = clusters[0]
        if counts[primary] >= soft_cap:
            deferred.append(item)
            continue
        selected.append(item)
        counts[primary] += 1
        if len(selected) == limit:
            return selected
    selected.extend(deferred[: limit - len(selected)])
    return selected


def _score_threshold(value: object) -> int:
    number = float(value)
    return round(number * 100) if number <= 1 else round(number)

