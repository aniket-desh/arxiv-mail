from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .arxiv import fetch_by_ids, fetch_daily_rss, fetch_recent_api
from .config import AppConfig, load_config
from .mailer import send_gmail
from .models import Digest, Paper
from .openai_api import OpenAIClient
from .ranking import assess_papers, shortlist
from .render import render_html, render_text, validate_html
from .secrets import Credentials, load_gmail_app_password
from .select import select_digest_papers
from .state import StateStore
from .zotero import ZoteroClient


def generate_digest(
    *,
    config_path: str | Path,
    state_path: str | Path,
    output_dir: str | Path,
    reserve: bool = False,
    source: str = "rss",
    lookback_hours: int | None = None,
    now: datetime | None = None,
) -> Digest:
    config = load_config(config_path)
    credentials = Credentials.load()
    local_now = now or datetime.now(ZoneInfo(str(config.delivery.get("timezone", "America/Chicago"))))
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=ZoneInfo(str(config.delivery.get("timezone", "America/Chicago"))))
    focus = config.focus_for(local_now.date())
    state = StateStore(state_path)
    papers, source_notice = _fetch_papers(config, state, source, local_now, lookback_hours)
    screened_count = len(papers)

    eligible: list[Paper] = []
    revision_ids: set[str] = set()
    for paper in papers:
        include, is_revision = state.eligibility(paper)
        if include:
            eligible.append(paper)
            if is_revision:
                revision_ids.add(paper.base_id)

    zotero_items = state.zotero_items()
    profile_notice = ""
    if reserve and not credentials.zotero_api_key:
        raise ValueError("ZOTERO_API_KEY is required for a hosted delivery reservation")
    if credentials.zotero_api_key:
        zotero = ZoteroClient(credentials.zotero_api_key)
        authenticated_user = ""
        cached_user = str(state.data.get("zotero", {}).get("user_id", ""))
        try:
            identity = zotero.identity()
            authenticated_user = identity[0]
            zotero_items = zotero.sync(state, identity=identity)
        except Exception as exc:
            cache_matches_key = bool(authenticated_user and authenticated_user == cached_user)
            if not zotero_items or not cache_matches_key:
                zotero_items = []
                if reserve:
                    raise RuntimeError(
                        "Zotero taste sync could not verify a matching cached profile; "
                        "refusing to reserve a digest"
                    ) from exc
                print(f"Warning: Zotero sync unavailable; continuing without library taste ({exc})")
            else:
                print(f"Warning: Zotero sync unavailable; using cached metadata ({exc})")
                profile_notice = "Zotero sync was unavailable, so ranking used the cached taste profile."

    openai = OpenAIClient(credentials.openai_api_key)
    candidates = shortlist(eligible, zotero_items, config, focus, openai) if eligible else []
    assessed = assess_papers(candidates, config, focus, openai) if candidates else []
    main, less_urgent, revisions = select_digest_papers(assessed, revision_ids, config)
    digest = Digest(
        digest_date=local_now.date(),
        run_id=uuid.uuid4().hex,
        monthly_focus=focus.description if focus else "Permanent research profile",
        screened_count=screened_count,
        main=main,
        less_urgent=less_urgent,
        revisions=revisions,
        notice=" ".join(part for part in (source_notice, profile_notice) if part),
        model=str(config.models.get("ranker", "gpt-5.6-luna")),
    )
    html, text = _write_outputs(digest, output_dir, assessed)

    if reserve:
        sender = str(config.delivery.get("sender") or config.delivery["recipient"])
        recipient = str(config.delivery["recipient"])
        fingerprint = (
            _message_fingerprint(
                run_id=digest.run_id,
                sender=sender,
                recipient=recipient,
                subject=_subject(digest),
                text=text,
                html=html,
            )
            if digest.selected_count
            else ""
        )
        state.record_run(
            digest,
            sent=False,
            reserved=bool(digest.selected_count),
            message_sha256=fingerprint,
        )
    state.save()
    return digest


def deliver_reserved_digest(
    *,
    config_path: str | Path,
    state_path: str | Path,
    output_dir: str | Path,
) -> tuple[bool, int]:
    config = load_config(config_path)
    gmail_app_password = load_gmail_app_password()
    output_path = Path(output_dir)
    payload = json.loads((output_path / "digest-data.json").read_text())
    digest_data = payload.get("digest", {})
    run_id = str(digest_data.get("run_id", ""))
    selected_count = sum(
        len(digest_data.get(section, ())) for section in ("main", "less_urgent", "revisions")
    )
    state = StateStore(state_path)
    run = state.run(run_id)
    if run is None:
        raise ValueError("Rendered digest has no matching reserved state")
    if selected_count == 0:
        if run.get("status") != "empty":
            raise ValueError("Empty rendered digest does not match its state")
        return False, 0
    if run.get("status") != "reserved":
        raise ValueError(f"Run {run_id} is not awaiting delivery")

    html = (output_path / "digest-preview.html").read_text()
    text = (output_path / "digest-preview.txt").read_text()
    validate_html(html)
    digest_date = date.fromisoformat(str(digest_data["digest_date"]))
    subject = _subject_for(digest_date, selected_count)
    sender = str(config.delivery.get("sender") or config.delivery["recipient"])
    recipient = str(config.delivery["recipient"])
    actual_fingerprint = _message_fingerprint(
        run_id=run_id,
        sender=sender,
        recipient=recipient,
        subject=subject,
        text=text,
        html=html,
    )
    expected_fingerprint = str(run.get("message_sha256", ""))
    if not expected_fingerprint or not hmac.compare_digest(actual_fingerprint, expected_fingerprint):
        raise ValueError("Rendered digest changed after its delivery reservation")

    send_gmail(
        sender=sender,
        recipient=recipient,
        app_password=gmail_app_password,
        subject=subject,
        text_body=text,
        html_body=html,
        run_id=run_id,
    )
    state.mark_run_delivered(run_id)
    state.save()
    return True, selected_count


def calibrate(
    *,
    config_path: str | Path,
    state_path: str | Path,
    output_dir: str | Path,
) -> tuple[Digest, list[dict[str, Any]]]:
    config = load_config(config_path)
    credentials = Credentials.load()
    state = StateStore(state_path)
    expected = {item.arxiv_id: item.tier for item in config.calibration}
    papers = fetch_by_ids(expected)
    by_base = {paper.base_id: paper for paper in papers}
    ordered = [by_base[item.arxiv_id] for item in config.calibration if item.arxiv_id in by_base]
    focus_day = config.monthly_focus[0].starts if config.monthly_focus else datetime.now().date()
    focus = config.focus_for(focus_day)
    zotero_items = []
    if credentials.zotero_api_key:
        try:
            zotero = ZoteroClient(credentials.zotero_api_key)
            identity = zotero.identity()
            zotero_items = zotero.sync(state, identity=identity)
        except Exception as exc:
            zotero_items = []
            print(f"Warning: Zotero sync unavailable during calibration ({exc})")
    openai = OpenAIClient(credentials.openai_api_key)
    candidates = shortlist(ordered, zotero_items, config, focus, openai)
    assessed = assess_papers(candidates, config, focus, openai)
    results: list[dict[str, Any]] = []
    for paper, local, assessment in assessed:
        results.append(
            {
                "arxiv_id": paper.base_id,
                "expected": expected.get(paper.base_id),
                "actual": assessment.tier,
                "score": assessment.score,
                "match": expected.get(paper.base_id) == assessment.tier,
                "why": assessment.why_match,
            }
        )
    revision_ids: set[str] = set()
    main, less, revisions = select_digest_papers(assessed, revision_ids, config)
    digest = Digest(
        digest_date=focus_day,
        run_id="calibration",
        monthly_focus=focus.description if focus else "Calibration profile",
        screened_count=len(ordered),
        main=main,
        less_urgent=less,
        revisions=revisions,
        model=str(config.models.get("ranker", "gpt-5.6-luna")),
    )
    _write_outputs(digest, output_dir, assessed, extra={"calibration": results})
    state.save()
    return digest, results


def _fetch_papers(
    config: AppConfig,
    state: StateStore,
    source: str,
    local_now: datetime,
    lookback_hours: int | None,
) -> tuple[list[Paper], str]:
    if source == "rss":
        daily = fetch_daily_rss(config.categories)
        last_run = state.last_completed_run
        now_utc = local_now.astimezone(timezone.utc)
        if last_run and now_utc - last_run > timedelta(hours=30):
            notice = (
                f"New submissions from {last_run.date().isoformat()} through "
                f"{now_utc.date().isoformat()} were backfilled through arXiv search; replacement "
                "and cross-list-only announcements during that interval may be absent."
            )
            print(f"Warning: {notice}")
            time.sleep(3.1)
            recovered = fetch_recent_api(
                config.categories,
                last_run,
                now_utc,
                max_results=int(config.retrieval.get("max_results", 2000)),
            )
            return _dedupe_papers([*daily, *recovered]), notice
        return daily, ""
    hours = lookback_hours or int(config.retrieval.get("lookback_hours", 72))
    last_run = state.last_completed_run
    lookback_start = local_now.astimezone(timezone.utc) - timedelta(hours=hours)
    since = lookback_start if lookback_hours is not None or not last_run else max(last_run, lookback_start)
    papers = fetch_recent_api(
        config.categories,
        since,
        local_now.astimezone(timezone.utc),
        max_results=int(config.retrieval.get("max_results", 2000)),
    )
    notice = (
        "This catch-up uses arXiv search for new submissions; replacement and "
        "cross-list-only announcements may be absent."
    )
    return papers, notice


def _dedupe_papers(papers: list[Paper]) -> list[Paper]:
    unique: dict[str, Paper] = {}
    for paper in papers:
        previous = unique.get(paper.base_id)
        if previous is None or paper.version > previous.version:
            unique[paper.base_id] = paper
            continue
        if paper.version != previous.version:
            continue
        unique[paper.base_id] = Paper(
            arxiv_id=previous.arxiv_id,
            base_id=previous.base_id,
            version=previous.version,
            title=previous.title or paper.title,
            authors=previous.authors or paper.authors,
            abstract=previous.abstract or paper.abstract,
            categories=tuple(dict.fromkeys((*previous.categories, *paper.categories))),
            primary_category=previous.primary_category or paper.primary_category,
            published=previous.published,
            updated=previous.updated,
            comments=previous.comments or paper.comments,
            journal_ref=previous.journal_ref or paper.journal_ref,
            announce_type=previous.announce_type,
        )
    return sorted(unique.values(), key=lambda paper: paper.published, reverse=True)


def _write_outputs(
    digest: Digest,
    output_dir: str | Path,
    assessed: list,
    *,
    extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    html = render_html(digest)
    text = render_text(digest)
    _write_private_text(output_path / "digest-preview.html", html)
    _write_private_text(output_path / "digest-preview.txt", text)
    payload: dict[str, Any] = {
        "digest": _jsonable(asdict(digest)),
        "assessed": [
            {
                "paper": _jsonable(asdict(paper)),
                "local_score": _public_local_score(local),
                "assessment": _jsonable(asdict(assessment)),
            }
            for paper, local, assessment in assessed
        ],
    }
    if extra:
        payload.update(extra)
    _write_private_text(
        output_path / "digest-data.json",
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    return html, text


def _subject(digest: Digest) -> str:
    return _subject_for(digest.digest_date, digest.selected_count)


def _subject_for(digest_date: date, selected_count: int) -> str:
    return f"arXiv brief — {digest_date:%a, %b %-d} — {selected_count} paper{'s' if selected_count != 1 else ''}"


def _message_fingerprint(
    *,
    run_id: str,
    sender: str,
    recipient: str,
    subject: str,
    text: str,
    html: str,
) -> str:
    fields = (run_id, sender, recipient, subject, text, html)
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def _write_private_text(path: Path, value: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(value)
    os.replace(temp_path, path)
    path.chmod(0o600)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _public_local_score(local: Any) -> dict[str, Any]:
    payload = asdict(local)
    payload.pop("nearest_zotero", None)
    return _jsonable(payload)
