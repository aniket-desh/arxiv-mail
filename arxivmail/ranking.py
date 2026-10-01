from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Iterable

from .config import AppConfig, Focus
from .models import Assessment, LocalScore, Paper, ZoteroItem
from .openai_api import OpenAIClient


SPACE_RE = re.compile(r"[^a-z0-9+]+")


def normalize(value: str) -> str:
    return " ".join(SPACE_RE.sub(" ", value.casefold()).split())


def lexical_score(paper: Paper, config: AppConfig, focus: Focus | None) -> LocalScore:
    title = normalize(paper.title)
    abstract = normalize(paper.abstract)
    matched_terms: list[str] = []
    matched_clusters: list[str] = []
    cluster_strengths: dict[str, float] = {}
    title_multiplier = float(config.ranking.get("title_phrase_multiplier", 1.2))
    abstract_multiplier = float(config.ranking.get("abstract_phrase_multiplier", 1.0))

    for cluster in config.clusters:
        evidence = 0.0
        for phrase in cluster.phrases:
            term = normalize(phrase)
            in_title = term and term in title
            in_abstract = term and term in abstract
            if in_title or in_abstract:
                matched_terms.append(phrase)
                evidence += title_multiplier if in_title else 0.0
                evidence += abstract_multiplier if in_abstract else 0.0
        if evidence:
            focus_boost = focus.cluster_boosts.get(cluster.id, 1.0) if focus else 1.0
            strength = (1.0 - math.exp(-evidence / 3.0)) * cluster.weight * focus_boost
            cluster_strengths[cluster.id] = strength
            matched_clusters.append(cluster.id)

    focus_hits: list[str] = []
    if focus:
        for phrase in focus.phrases:
            term = normalize(phrase)
            if term and (term in title or term in abstract):
                focus_hits.append(phrase)

    lexical = min(1.0, sum(cluster_strengths.values()) / 2.2)
    focus_score = min(1.0, len(set(focus_hits)) / 3.0) if focus else 0.0
    lexical = min(1.0, lexical + focus_score * 0.25 * (focus.weight if focus else 1.0))

    rnla = "randomized_numerical_linear_algebra" in cluster_strengths
    active_quantum = {
        "tensor_networks_algorithms",
        "quantum_many_body_information",
        "quantum_chemistry_electronic_structure",
    } & set(matched_clusters)
    if rnla and not active_quantum:
        lexical *= float(config.ranking.get("legacy_numerical_linear_algebra_multiplier", 0.3))

    return LocalScore(
        lexical=lexical,
        matched_terms=tuple(dict.fromkeys(matched_terms))[:14],
        matched_clusters=tuple(matched_clusters),
        focus_hits=tuple(dict.fromkeys(focus_hits))[:10],
    )


def shortlist(
    papers: Iterable[Paper],
    zotero_items: Iterable[ZoteroItem],
    config: AppConfig,
    focus: Focus | None,
    client: OpenAIClient,
) -> list[tuple[Paper, LocalScore]]:
    scored = [(paper, lexical_score(paper, config, focus)) for paper in papers]
    embedding_pool = scored
    if not embedding_pool:
        return []

    anchors = [
        f"{cluster.name}. " + "; ".join(cluster.phrases)
        for cluster in config.clusters
    ]
    anchors.extend(
        f"INTEREST BRIDGE: {query}" for query in config.bridges.get("queries", ())
    )
    if focus:
        anchors.append(f"CURRENT MONTH PRIORITY: {focus.description}. " + "; ".join(focus.phrases))

    zotero = [item for item in zotero_items if item.title][:160]
    paper_texts = [f"{paper.title}\n{paper.abstract}"[:9000] for paper, _ in embedding_pool]
    texts = anchors + paper_texts + [item.embedding_text[:9000] for item in zotero]
    vectors = client.embeddings(texts, model=str(config.models.get("embedding", "text-embedding-3-small")))
    anchor_vectors = vectors[: len(anchors)]
    paper_start = len(anchors)
    paper_vectors = vectors[paper_start : paper_start + len(paper_texts)]
    zotero_vectors = vectors[paper_start + len(paper_texts) :]

    semantic_weight = float(config.ranking.get("semantic_weight", 0.48))
    taxonomy_weight = float(config.ranking.get("taxonomy_weight", 0.18))
    focus_weight = float(config.ranking.get("monthly_focus_weight", 0.16))
    zotero_weight = float(config.ranking.get("zotero_weight", 0.08))
    ranked: list[tuple[Paper, LocalScore]] = []
    for (paper, local), vector in zip(embedding_pool, paper_vectors, strict=True):
        similarities = [_cosine(vector, anchor) for anchor in anchor_vectors]
        semantic = _scale_similarity(max(similarities[:-1] if focus else similarities))
        focus_semantic = _scale_similarity(similarities[-1]) if focus else 0.0
        nearest: list[tuple[float, str]] = []
        for item, saved_vector in zip(zotero, zotero_vectors, strict=True):
            nearest.append((_cosine(vector, saved_vector), item.title))
        nearest.sort(reverse=True)
        zotero_similarity = _scale_similarity(nearest[0][0]) if nearest else 0.0
        combined = (
            semantic_weight * semantic
            + taxonomy_weight * local.lexical
            + focus_weight * focus_semantic
            + zotero_weight * zotero_similarity
        )
        updated = replace(
            local,
            semantic=semantic,
            zotero=zotero_similarity,
            combined=combined,
            nearest_zotero=tuple(title for _, title in nearest[:3]),
        )
        ranked.append((paper, updated))
    ranked.sort(key=lambda pair: pair[1].combined, reverse=True)
    return _balanced_llm_pool(ranked, config)


def assess_papers(
    candidates: list[tuple[Paper, LocalScore]],
    config: AppConfig,
    focus: Focus | None,
    client: OpenAIClient,
) -> list[tuple[Paper, LocalScore, Assessment]]:
    if not candidates:
        return []
    batch_size = int(config.retrieval.get("rank_batch_size", 24))
    all_results: list[tuple[Paper, LocalScore, Assessment]] = []
    instructions = _ranking_instructions(config, focus)
    schema = _assessment_schema()
    model = str(config.models.get("ranker", "gpt-5.6-luna"))
    effort = str(config.models.get("ranker_reasoning_effort", "low"))

    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        records = []
        for paper, local in batch:
            record = paper.prompt_record()
            record["retrieval_evidence"] = {
                "matched_terms": list(local.matched_terms),
                "matched_clusters": list(local.matched_clusters),
                "monthly_focus_hits": list(local.focus_hits),
                "semantic_score": round(local.semantic, 3),
                "zotero_similarity": round(local.zotero, 3),
                "nearest_saved_titles": list(local.nearest_zotero),
            }
            records.append(record)
        response = client.structured_response(
            model=model,
            instructions=instructions,
            input_text=json.dumps({"papers": records}, ensure_ascii=False),
            schema_name="arxiv_paper_assessments",
            schema=schema,
            reasoning_effort=effort,
            max_output_tokens=24000,
        )
        by_id = {paper.arxiv_id: (paper, local) for paper, local in batch}
        seen: set[str] = set()
        for item in response.get("assessments", []):
            arxiv_id = str(item["arxiv_id"])
            if arxiv_id not in by_id or arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            paper, local = by_id[arxiv_id]
            assessment = Assessment(
                arxiv_id=arxiv_id,
                tier=str(item["tier"]),
                score=int(item["score"]),
                confidence=float(item["confidence"]),
                fit_type=str(item["fit_type"]),
                matched_clusters=tuple(item["matched_clusters"]),
                monthly_focus_hits=tuple(item["monthly_focus_hits"]),
                why_match=_concise_why_match(str(item["why_match"])),
                summary_sentences=tuple(item["summary_sentences"]),
                reading_value=str(item["reading_value"]),
            )
            all_results.append((paper, local, assessment))
        missing = set(by_id) - seen
        if missing:
            raise RuntimeError(f"Ranker omitted {len(missing)} paper(s) from a structured batch")
    return all_results


def _ranking_instructions(config: AppConfig, focus: Focus | None) -> str:
    calibrations = [
        {"id": item.arxiv_id, "tier": item.tier, "reason": item.reason}
        for item in config.calibration
    ]
    cluster_summary = [{"id": item.id, "name": item.name, "weight": item.weight} for item in config.clusters]
    focus_text = focus.description if focus else "No temporary monthly focus is active."
    return f"""
You are the final abstract-aware curator for one theoretical-physics/ML researcher. Assess every supplied paper exactly once. Base claims only on the supplied title, abstract, metadata, and retrieval evidence; never invent a result. Retrieval evidence is a hint, not a decision.

Permanent center: statistical physics of learning, disorder, theoretical neuroscience, mechanistic interpretability, tensor networks, and current quantum many-body/electronic-structure work. Current focus: {focus_text}

Use M for genuinely urgent Read first papers, P for useful but adjacent Less urgent papers, and N for papers that should not appear. M requires a substantive method/result connection, not shared vocabulary. Physics of learning is core. Physics-inspired ML is strong only when physics supplies real analysis or mechanism. ML for physics is normally secondary unless its method or result transfers to the researcher's current work. Generic randomized numerical linear algebra is legacy evidence unless it directly enables tensor-network contraction, eigensolvers, many-body simulation, or electronic structure. Generic attention variants are not M merely because they invoke entropy or statistical mechanics.

The fit_type describes the paper's own technical contribution, not why the researcher likes it. Use physics_of_learning only when statistical-physics concepts or methods analyze learning; ordinary mechanistic-interpretability, alignment, or learning-theory papers without that mechanism are other.

The why_match field is separate from the summary: write exactly one concise sentence of at most 45 words naming concrete methods, results, or ideas, and do not repeat the whole summary there. Write exactly 3 or 4 concise technical summary_sentences: problem, method, main result, then limitation or reason to care. The reading_value field is a short concrete verdict. Scores are 0-100 and confidence is 0-1.

Calibration examples (explicit user labels outweigh superficial title/category cues): {json.dumps(calibrations)}
Cluster vocabulary summary: {json.dumps(cluster_summary)}
""".strip()


def _assessment_schema() -> dict:
    item_properties = {
        "arxiv_id": {"type": "string"},
        "tier": {"type": "string", "enum": ["M", "P", "N"]},
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "fit_type": {
            "type": "string",
            "enum": ["physics_of_learning", "physics_inspired_ml", "ml_for_physics", "other"],
        },
        "matched_clusters": {"type": "array", "items": {"type": "string"}},
        "monthly_focus_hits": {"type": "array", "items": {"type": "string"}},
        "why_match": {
            "type": "string",
            "description": "Exactly one sentence, at most 45 words, naming concrete matching methods or results.",
        },
        "summary_sentences": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 4,
        },
        "reading_value": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": item_properties,
                    "required": list(item_properties),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["assessments"],
        "additionalProperties": False,
    }


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _scale_similarity(value: float) -> float:
    return max(0.0, min(1.0, (value - 0.18) / 0.62))


def _concise_why_match(value: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", " ".join(value.split()))
    if len(sentences) <= 1:
        combined = sentences[0] if sentences else value.strip()
    else:
        first = sentences[0].rstrip(".!?")
        second = sentences[1].strip()
        if second:
            second = second[0].lower() + second[1:]
        combined = f"{first}; {second}"
    words = combined.split()
    if len(words) > 50:
        combined = " ".join(words[:50]).rstrip(".,;:") + "…"
    elif combined and combined[-1] not in ".!?…":
        combined += "."
    return combined


def _balanced_llm_pool(
    ranked: list[tuple[Paper, LocalScore]], config: AppConfig
) -> list[tuple[Paper, LocalScore]]:
    limit = int(config.retrieval.get("llm_limit", 48))
    calibration_ids = {item.arxiv_id for item in config.calibration}
    chosen: dict[str, tuple[Paper, LocalScore]] = {}

    def add(pair: tuple[Paper, LocalScore]) -> None:
        if len(chosen) < limit:
            chosen.setdefault(pair[0].base_id, pair)

    for pair in ranked:
        if pair[0].base_id in calibration_ids:
            add(pair)
    for pair in (item for item in ranked if item[1].focus_hits):
        add(pair)
        if sum(bool(item[1].focus_hits) for item in chosen.values()) >= min(16, limit // 2):
            break
    for cluster in config.clusters:
        for pair in ranked:
            if cluster.id in pair[1].matched_clusters:
                add(pair)
                break
    for pair in ranked:
        add(pair)
        if len(chosen) >= limit:
            break
    return sorted(chosen.values(), key=lambda pair: pair[1].combined, reverse=True)
