"""Render a compact, Gmail-safe arXiv digest.

The renderer intentionally depends on object shape rather than concrete model
classes.  Dataclasses, Pydantic models, simple namespaces, and dictionaries are
all accepted as long as they expose the documented digest and paper fields.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from html import escape
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urlparse


MAX_HTML_BYTES = 90 * 1024
_MISSING = object()


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first present, non-None field from an object or mapping."""

    for name in names:
        if isinstance(obj, Mapping):
            value = obj.get(name, _MISSING)
        else:
            value = getattr(obj, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def _paper(item: Any) -> Any:
    return _value(item, "paper", "record", "source", default=item)


def _paper_value(item: Any, *names: str, default: Any = None) -> Any:
    value = _value(item, *names, default=_MISSING)
    if value is not _MISSING:
        return value
    assessment = _value(item, "assessment", default=None)
    if assessment is not None:
        value = _value(assessment, *names, default=_MISSING)
        if value is not _MISSING:
            return value
    return _value(_paper(item), *names, default=default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        value = value.value
    return " ".join(str(value).split()) or default


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _format_date(value: Any) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return f"{value.strftime('%a, %b')} {value.day}, {value.year}"
    raw = _text(value)
    if not raw:
        return "Today"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return f"{parsed.strftime('%a, %b')} {parsed.day}, {parsed.year}"


def _join_text(value: Any, *, limit: int | None = None) -> str:
    values = _items(value)
    rendered: list[str] = []
    for entry in values:
        rendered.append(_text(_value(entry, "name", default=entry)))
    rendered = [part for part in rendered if part]
    if limit is not None and len(rendered) > limit:
        hidden = len(rendered) - limit
        return ", ".join(rendered[:limit]) + f", and {hidden} more"
    return ", ".join(rendered)


def _arxiv_id(item: Any) -> str:
    raw = _text(
        _paper_value(
            item,
            "arxiv_id",
            "id",
            "identifier",
            "entry_id",
            default="",
        )
    )
    if not raw:
        raw = _text(
            _paper_value(item, "abstract_url", "abs_url", "url", default="")
        )
    if "://" in raw:
        path = urlparse(raw).path
        raw = re.sub(r"^/(?:abs|pdf|html)/", "", path)
    raw = raw.removeprefix("arXiv:").removesuffix(".pdf").strip(" /")
    return raw


def _version(item: Any) -> str:
    explicit = _text(_paper_value(item, "version", default=""))
    if explicit:
        return explicit if explicit.lower().startswith("v") else f"v{explicit}"
    match = re.search(r"(v\d+)$", _arxiv_id(item), flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _arxiv_url(item: Any, kind: str) -> str:
    field_names = {
        "abs": ("abstract_url", "abs_url", "url"),
        "html": ("html_url",),
        "pdf": ("pdf_url",),
    }[kind]
    supplied = _text(_paper_value(item, *field_names, default=""))
    if supplied.startswith("https://arxiv.org/"):
        return supplied
    identifier = _arxiv_id(item)
    return f"https://arxiv.org/{kind}/{identifier}"


def _authors(item: Any) -> str:
    return _join_text(_paper_value(item, "authors", "author_names", default=[])) or "Authors unavailable"


def _categories(item: Any) -> str:
    return _join_text(
        _paper_value(item, "categories", "category", "primary_category", default=[])
    )


def _summary(item: Any) -> str:
    value = _paper_value(
        item,
        "summary",
        "technical_summary",
        "digest_summary",
        "synopsis",
        "summary_sentences",
        default="",
    )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return " ".join(filter(None, (_text(sentence) for sentence in value)))
    return _text(value)


def _why_match(item: Any) -> str:
    return _text(
        _paper_value(
            item,
            "why_match",
            "why_it_matches",
            "match_reason",
            "rationale",
            "reason",
            default="",
        )
    )


def _status_parts(item: Any) -> list[str]:
    version = _version(item)
    status = _text(
        _paper_value(item, "status", "revision_status", "announce_type", default="")
    )
    if not status:
        match = re.fullmatch(r"v(\d+)", version, flags=re.IGNORECASE)
        status = "Revised" if match and int(match.group(1)) > 1 else "New"
    parts = [version, status]
    categories = _categories(item)
    if categories:
        parts.append(categories)
    announced = _paper_value(
        item,
        "announcement_date",
        "announced_at",
        "published",
        "updated",
        default=None,
    )
    if announced:
        parts.append(_format_date(announced))
    return [part for part in parts if part]


def _section_name(item: Any) -> str:
    return _text(
        _paper_value(item, "section", "bucket", "tier", "priority", "label", default="")
    ).lower()


def _sections(digest: Any) -> tuple[list[Any], list[Any], list[Any]]:
    revisions = _items(
        _value(
            digest,
            "important_revisions",
            "revisions",
            "material_revisions",
            default=None,
        )
    )
    read_first = _items(
        _value(
            digest,
            "read_first",
            "strong_papers",
            "must_read",
            "top_papers",
            "main",
            default=None,
        )
    )
    less_urgent = _items(
        _value(digest, "less_urgent", "optional_papers", "secondary_papers", default=None)
    )

    if not (revisions or read_first or less_urgent):
        ranked = _items(_value(digest, "papers", "items", "ranked_papers", default=[]))
        for item in ranked:
            section = _section_name(item)
            important_revision = bool(
                _paper_value(item, "important_revision", "is_important_revision", default=False)
            )
            if important_revision or "important revision" in section or section == "revision":
                revisions.append(item)
            elif section in {"n", "none", "exclude", "excluded", "skip"}:
                continue
            elif section in {"p", "less urgent", "less_urgent", "optional", "secondary"}:
                less_urgent.append(item)
            else:
                read_first.append(item)

    seen: set[str] = set()

    def unique(items: list[Any]) -> list[Any]:
        result: list[Any] = []
        for item in items:
            key = _arxiv_id(item) or _text(_paper_value(item, "title", default=""))
            if not key:
                key = str(id(item))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    revisions = unique(revisions)
    read_first = unique(read_first)
    less_urgent = unique(less_urgent)[:3]
    return revisions, read_first, less_urgent


def _focus(digest: Any) -> str:
    value = _value(
        digest,
        "monthly_focus",
        "current_focus",
        "focus",
        "focus_profile",
        default="",
    )
    if isinstance(value, Mapping):
        value = _value(value, "description", "summary", "name", default=list(value.values()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "; ".join(filter(None, (_text(part) for part in value)))
    return _text(value)


def _notice(digest: Any) -> str:
    return _text(_value(digest, "notice", "source_notice", "recovery_notice", default=""))


def _card(item: Any, *, index: int, section: str, accent: str, tint: str) -> str:
    title = escape(_text(_paper_value(item, "title", default="Untitled paper")))
    authors = escape(_authors(item))
    metadata = escape(" · ".join(_status_parts(item)))
    why = escape(_why_match(item))
    summary = escape(_summary(item))
    identifier = escape(_arxiv_id(item), quote=True)
    abs_url = escape(_arxiv_url(item, "abs"), quote=True)
    html_url = escape(_arxiv_url(item, "html"), quote=True)
    pdf_url = escape(_arxiv_url(item, "pdf"), quote=True)
    label = escape(section.upper())

    return f"""
<table role="presentation" class="paper-card" data-arxiv-id="{identifier}" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:separate;background:#ffffff;border:1px solid #dfe3e8;border-radius:12px;margin:0 0 14px 0;">
  <tr>
    <td style="width:5px;background:{accent};border-radius:12px 0 0 12px;font-size:0;line-height:0;">&nbsp;</td>
    <td class="card-pad" style="padding:20px 22px 19px 22px;">
      <p style="margin:0 0 9px 0;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:15px;font-weight:700;letter-spacing:0.08em;color:{accent};">{index} &nbsp;·&nbsp; {label}</p>
      <h3 class="paper-title" style="margin:0 0 7px 0;font-family:Arial,Helvetica,sans-serif;font-size:20px;line-height:27px;font-weight:700;color:#172033;overflow-wrap:anywhere;">{title}</h3>
      <p style="margin:0 0 7px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:21px;color:#4f596b;">{authors}</p>
      <p style="margin:0 0 15px 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;color:#687386;">{metadata}</p>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:separate;background:{tint};border-radius:8px;margin:0 0 14px 0;">
        <tr>
          <td style="padding:12px 14px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:21px;color:#263247;">
            <strong style="font-weight:700;color:#172033;">Why it matches:</strong>
            <span class="why-match">{why}</span>
          </td>
        </tr>
      </table>
      <p class="paper-summary" style="margin:0 0 16px 0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:24px;color:#263247;">{summary}</p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:separate;">
        <tr>
          <td class="link-cell" style="padding:0 8px 0 0;">
            <a href="{abs_url}" style="display:inline-block;padding:10px 13px;border-radius:7px;background:#172033;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;font-weight:700;color:#ffffff;text-decoration:none;">Abstract</a>
          </td>
          <td class="link-cell" style="padding:0 8px 0 0;">
            <a href="{html_url}" style="display:inline-block;padding:10px 13px;border:1px solid #c9d0da;border-radius:7px;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;font-weight:700;color:#263247;text-decoration:none;">HTML</a>
          </td>
          <td class="link-cell" style="padding:0;">
            <a href="{pdf_url}" style="display:inline-block;padding:10px 13px;border:1px solid #c9d0da;border-radius:7px;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:18px;font-weight:700;color:#263247;text-decoration:none;">PDF</a>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def _section(
    title: str,
    subtitle: str,
    papers: list[Any],
    accent: str,
    tint: str,
    *,
    start: int,
) -> tuple[str, int]:
    if not papers:
        return "", start
    cards: list[str] = []
    index = start
    for paper in papers:
        cards.append(_card(paper, index=index, section=title, accent=accent, tint=tint))
        index += 1
    heading = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;margin:25px 0 11px 0;">
  <tr>
    <td>
      <h2 style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:17px;line-height:23px;font-weight:700;color:#172033;">{escape(title)}</h2>
      <p style="margin:3px 0 0 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:19px;color:#687386;">{escape(subtitle)}</p>
    </td>
    <td align="right" style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;font-weight:700;color:{accent};">{len(papers)} paper{'s' if len(papers) != 1 else ''}</td>
  </tr>
</table>"""
    return heading + "".join(cards), index


def render_html(digest: Any) -> str:
    """Render *digest* as self-contained HTML suitable for Gmail."""

    revisions, read_first, less_urgent = _sections(digest)
    selected = len(revisions) + len(read_first) + len(less_urgent)
    screened = _value(digest, "screened_count", "candidate_count", "screened", default=selected)
    try:
        screened_label = f"{int(screened):,}"
    except (TypeError, ValueError):
        screened_label = escape(_text(screened, default=str(selected)))
    digest_date = _format_date(
        _value(digest, "date", "digest_date", "generated_for", "generated_at", default=None)
    )
    focus = _focus(digest)
    notice = _notice(digest)
    preheader = f"{selected} papers selected from {screened_label} screened."
    if focus:
        preheader += f" Current focus: {focus}"
    if notice:
        preheader += f" {notice}"

    chunks: list[str] = []
    index = 1
    for args in (
        (
            "Important revisions",
            "Previously seen papers with a material update worth reopening.",
            revisions,
            "#6d4aff",
            "#f2efff",
        ),
        (
            "Read first",
            "The strongest matches for your active research interests.",
            read_first,
            "#1769aa",
            "#edf6fc",
        ),
        (
            "Less urgent",
            "Relevant and worth saving, but lower priority for today.",
            less_urgent,
            "#a15c00",
            "#fff5e7",
        ),
    ):
        section_html, index = _section(*args, start=index)
        chunks.append(section_html)

    focus_html = ""
    if focus:
        focus_html = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:separate;background:#f1f4f8;border-radius:9px;margin:17px 0 4px 0;">
  <tr>
    <td style="padding:12px 14px;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:20px;color:#3f4a5f;">
      <strong style="font-weight:700;color:#172033;">This month:</strong> {escape(focus)}
    </td>
  </tr>
</table>"""

    notice_html = ""
    if notice:
        notice_html = f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:separate;background:#fff5e7;border-radius:9px;margin:12px 0 4px 0;">
  <tr>
    <td style="padding:12px 14px;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:20px;color:#70410a;">
      <strong style="font-weight:700;color:#5b3305;">Source note:</strong> {escape(notice)}
    </td>
  </tr>
</table>"""

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="supported-color-schemes" content="light">
  <title>arXiv brief — {escape(digest_date)}</title>
  <style>
    body {{ margin:0 !important; padding:0 !important; width:100% !important; background:#f3f5f7; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
    a[x-apple-data-detectors] {{ color:inherit !important; text-decoration:none !important; }}
    @media only screen and (max-width:680px) {{
      .shell {{ width:100% !important; }}
      .outer-pad {{ padding:14px 10px !important; }}
      .content-pad {{ padding-left:17px !important; padding-right:17px !important; }}
      .card-pad {{ padding:17px 16px 17px 16px !important; }}
      .paper-title {{ font-size:18px !important; line-height:25px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;width:100%;background:#f3f5f7;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">{escape(preheader)}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;background:#f3f5f7;">
    <tr>
      <td class="outer-pad" align="center" style="padding:28px 14px;">
        <table role="presentation" class="shell" width="640" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:640px;border-collapse:separate;background:#ffffff;border:1px solid #dfe3e8;border-radius:14px;">
          <tr>
            <td class="content-pad" style="padding:30px 31px 13px 31px;">
              <p style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;font-weight:700;letter-spacing:0.09em;color:#1769aa;">ARXIV BRIEF &nbsp;·&nbsp; {escape(digest_date.upper())}</p>
              <h1 style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:29px;line-height:36px;font-weight:700;color:#172033;">Today’s papers</h1>
              <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:21px;color:#687386;">{screened_label} screened &nbsp;·&nbsp; {selected} selected</p>
              {focus_html}
              {notice_html}
              {''.join(chunks)}
            </td>
          </tr>
          <tr>
            <td class="content-pad" style="padding:17px 31px 28px 31px;border-top:1px solid #e5e8ed;">
              <p style="margin:0 0 6px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:20px;font-weight:700;color:#172033;">Tune tomorrow’s ranking</p>
              <p style="margin:0 0 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:20px;color:#687386;">Save papers you want to keep in Zotero; the next profile sync uses their titles, abstracts, tags, and collections as a taste signal. Explicit M/P/N reply learning can be enabled after the first test email.</p>
              <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:17px;color:#8992a3;">Thank you to arXiv for use of its open access interoperability.</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    validate_html(html)
    return html


def render_text(digest: Any) -> str:
    """Render a complete plain-text alternative for *digest*."""

    revisions, read_first, less_urgent = _sections(digest)
    selected = len(revisions) + len(read_first) + len(less_urgent)
    screened = _value(digest, "screened_count", "candidate_count", "screened", default=selected)
    digest_date = _format_date(
        _value(digest, "date", "digest_date", "generated_for", "generated_at", default=None)
    )
    lines = ["ARXIV BRIEF", digest_date, "", f"{screened} screened · {selected} selected"]
    focus = _focus(digest)
    if focus:
        lines.extend([f"This month: {focus}", ""])
    notice = _notice(digest)
    if notice:
        lines.extend([f"Source note: {notice}", ""])

    index = 1
    for title, papers in (
        ("IMPORTANT REVISIONS", revisions),
        ("READ FIRST", read_first),
        ("LESS URGENT", less_urgent),
    ):
        if not papers:
            continue
        lines.extend([title, "=" * len(title)])
        for item in papers:
            lines.extend(
                [
                    f"{index}. {_text(_paper_value(item, 'title', default='Untitled paper'))}",
                    _authors(item),
                    " · ".join(_status_parts(item)),
                    f"Why it matches: {_why_match(item)}",
                    _summary(item),
                    f"Abstract: {_arxiv_url(item, 'abs')}",
                    f"HTML: {_arxiv_url(item, 'html')}",
                    f"PDF: {_arxiv_url(item, 'pdf')}",
                    "",
                ]
            )
            index += 1

    lines.extend(
        [
            "Tune tomorrow’s ranking",
            "Save papers you want to keep in Zotero; the next profile sync uses their titles, abstracts, tags, and collections as a taste signal.",
            "Explicit M/P/N reply learning can be enabled after the first test email.",
            "",
            "Thank you to arXiv for use of its open access interoperability.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _sentence_count(text: str) -> int:
    text = " ".join(text.split())
    if not text:
        return 0
    # Avoid treating common scholarly abbreviations as sentence boundaries.
    protected = re.sub(
        r"\b(?:e\.g|i\.e|Fig|Eq|Sec|Dr|Prof|vs|cf)\.",
        lambda match: match.group(0).replace(".", "∯"),
        text,
        flags=re.IGNORECASE,
    )
    protected = re.sub(r"\bet al\.", "et al∯", protected, flags=re.IGNORECASE)
    endings = re.findall(r"[.!?](?:[\"'”’\)\]]+)?(?=\s+[A-Z0-9\"“(\[]|$)", protected)
    return len(endings) or 1


class _HTMLAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.viewport = False
        self.paper_cards = 0
        self.paper_titles = 0
        self.why_matches = 0
        self.summaries: list[str] = []
        self.links: list[str] = []
        self.forbidden_tags: set[str] = set()
        self._summary_depth: int | None = None
        self._summary_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        attr_map = {key.lower(): value or "" for key, value in attrs}
        classes = set(attr_map.get("class", "").split())
        if tag.lower() in {"script", "form", "iframe", "object", "embed", "input", "img"}:
            self.forbidden_tags.add(tag.lower())
        if tag.lower() == "meta" and attr_map.get("name", "").lower() == "viewport":
            self.viewport = "width=device-width" in attr_map.get("content", "").lower()
        if tag.lower() == "a":
            self.links.append(attr_map.get("href", ""))
        if "paper-card" in classes:
            self.paper_cards += 1
        if "paper-title" in classes:
            self.paper_titles += 1
        if "why-match" in classes:
            self.why_matches += 1
        if "paper-summary" in classes:
            self._summary_depth = self.depth
            self._summary_chunks = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._summary_depth is not None:
            self._summary_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._summary_depth == self.depth:
            self.summaries.append(" ".join("".join(self._summary_chunks).split()))
            self._summary_depth = None
            self._summary_chunks = []
        self.depth = max(0, self.depth - 1)


def validate_html(html: str) -> None:
    """Raise ``ValueError`` when rendered email violates delivery constraints."""

    errors: list[str] = []
    size = len(html.encode("utf-8"))
    if size >= MAX_HTML_BYTES:
        errors.append(f"HTML is {size} bytes; it must stay below {MAX_HTML_BYTES} bytes")
    lowered = html.lower()
    if "max-width:640px" not in lowered:
        errors.append("missing the 640px single-column container")
    if re.search(r"(?:display\s*:\s*(?:grid|flex)|javascript\s*:)", lowered):
        errors.append("contains email-unsafe CSS or a JavaScript URL")

    audit = _HTMLAudit()
    try:
        audit.feed(html)
        audit.close()
    except Exception as exc:  # HTMLParser may surface malformed entities.
        errors.append(f"HTML parsing failed: {exc}")

    if not audit.viewport:
        errors.append("missing a width=device-width viewport meta tag")
    if audit.forbidden_tags:
        errors.append("contains forbidden tags: " + ", ".join(sorted(audit.forbidden_tags)))
    if audit.paper_titles != audit.paper_cards:
        errors.append("every paper card must have exactly one title")
    if audit.why_matches != audit.paper_cards:
        errors.append("every paper card must have exactly one why-it-matches explanation")
    if len(audit.summaries) != audit.paper_cards:
        errors.append("every paper card must have exactly one summary")
    for index, summary in enumerate(audit.summaries, start=1):
        count = _sentence_count(summary)
        if count not in {3, 4}:
            errors.append(f"paper {index} summary has {count} sentences; expected 3 or 4")
    for href in audit.links:
        if not re.fullmatch(r"https://arxiv\.org/(?:abs|html|pdf)/[^\s\"<>]+", href):
            errors.append(f"paper link is not an absolute arXiv URL: {href!r}")

    if errors:
        raise ValueError("Invalid digest HTML: " + "; ".join(errors))
