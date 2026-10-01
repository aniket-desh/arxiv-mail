from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import unittest

from arxivmail.render import MAX_HTML_BYTES, render_html, render_text, validate_html


SUMMARY = (
    "The paper studies associative memory with higher-order interactions in a disordered network. "
    "It uses dynamical mean-field theory and many-body simulations to derive the storage transition. "
    "The authors find a capacity regime that differs from the ordinary Hopfield model. "
    "This makes it directly useful for comparing replica-style learning theories with modern memory architectures."
)


@dataclass
class Paper:
    arxiv_id: str
    title: str
    authors: list[str]
    version: int
    status: str
    categories: list[str]
    announcement_date: date


@dataclass
class RankedPaper:
    paper: Paper
    tier: str
    why_match: str
    summary: str


@dataclass
class Digest:
    date: date
    screened_count: int
    monthly_focus: str
    read_first: list[RankedPaper] = field(default_factory=list)
    less_urgent: list[RankedPaper] = field(default_factory=list)
    important_revisions: list[RankedPaper] = field(default_factory=list)


def ranked(identifier: str, title: str, tier: str = "M", version: int = 1) -> RankedPaper:
    return RankedPaper(
        paper=Paper(
            arxiv_id=identifier,
            title=title,
            authors=["Ada Lovelace", "Emmy Noether"],
            version=version,
            status="revised" if version > 1 else "new",
            categories=["cond-mat.dis-nn", "cs.LG"],
            announcement_date=date(2026, 9, 6),
        ),
        tier=tier,
        why_match="Replica theory, associative memory, and explicit higher-order interactions match the current focus.",
        summary=SUMMARY,
    )


def sample_digest() -> Digest:
    return Digest(
        date=date(2026, 9, 6),
        screened_count=284,
        monthly_focus="Replica, cavity, and perturbation theory for learning and associative memory",
        important_revisions=[ranked("2601.00001v2", "A material revision", version=2)],
        read_first=[ranked("2602.00002v1", "Higher-order associative memories")],
        less_urgent=[ranked("2603.00003v1", "A physics-informed attention variant", tier="P")],
    )


class RenderTests(unittest.TestCase):
    def test_render_html_has_all_sections_and_gmail_safe_links(self) -> None:
        html = render_html(sample_digest())

        self.assertIn("Important revisions", html)
        self.assertIn("Read first", html)
        self.assertIn("Less urgent", html)
        self.assertIn("284 screened", html)
        self.assertIn("This month:", html)
        self.assertEqual(html.count('class="paper-card"'), 3)
        self.assertIn("https://arxiv.org/abs/2602.00002v1", html)
        self.assertIn("https://arxiv.org/html/2602.00002v1", html)
        self.assertIn("https://arxiv.org/pdf/2602.00002v1", html)
        self.assertIn("max-width:640px", html)
        self.assertLess(len(html.encode("utf-8")), MAX_HTML_BYTES)
        self.assertNotIn("display:flex", html)
        self.assertNotIn("display:grid", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertIsNone(validate_html(html))

    def test_render_html_escapes_untrusted_paper_text(self) -> None:
        digest = sample_digest()
        digest.read_first[0].paper.title = '<script>alert("x")</script> Replica & cavity'
        digest.read_first[0].why_match = "Methods < results & ideas"

        html = render_html(digest)

        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertIn("Methods &lt; results &amp; ideas", html)
        validate_html(html)

    def test_render_text_contains_complete_card_and_feedback_key(self) -> None:
        text = render_text(sample_digest())

        self.assertIn("IMPORTANT REVISIONS", text)
        self.assertIn("READ FIRST", text)
        self.assertIn("LESS URGENT", text)
        self.assertIn("Why it matches: Replica theory", text)
        self.assertIn(SUMMARY, text)
        self.assertIn("Abstract: https://arxiv.org/abs/2602.00002v1", text)
        self.assertIn("M/P/N reply learning", text)

    def test_renderer_caps_less_urgent_at_three_and_deduplicates_sections(self) -> None:
        duplicate = ranked("2601.00001v2", "A material revision", version=2)
        digest = sample_digest()
        digest.read_first.append(duplicate)
        digest.less_urgent = [
            ranked(f"2604.0000{i}v1", f"Optional paper {i}", tier="P") for i in range(5)
        ]

        html = render_html(digest)

        self.assertEqual(html.count('class="paper-card"'), 5)
        self.assertEqual(html.count('data-arxiv-id="2601.00001v2"'), 1)
        self.assertIn("Optional paper 2", html)
        self.assertNotIn("Optional paper 3", html)

    def test_validate_html_rejects_wrong_summary_length(self) -> None:
        digest = sample_digest()
        digest.read_first[0].summary = "Only one sentence is present."

        with self.assertRaisesRegex(ValueError, "expected 3 or 4"):
            render_html(digest)

    def test_validate_html_rejects_unsafe_link_and_oversized_message(self) -> None:
        html = render_html(sample_digest())

        with self.assertRaisesRegex(ValueError, "absolute arXiv URL"):
            validate_html(html.replace("https://arxiv.org/abs/", "javascript:"))

        oversized = html + (" " * MAX_HTML_BYTES)
        with self.assertRaisesRegex(ValueError, "must stay below"):
            validate_html(oversized)

    def test_flat_papers_are_grouped_by_tier(self) -> None:
        digest = {
            "date": "2026-09-06",
            "screened_count": 10,
            "monthly_focus": ["replica methods", "many-body learning"],
            "papers": [
                {**ranked("2605.00001v1", "Core").__dict__, "tier": "M"},
                {**ranked("2605.00002v1", "Secondary", tier="P").__dict__, "tier": "P"},
                {**ranked("2605.00003v1", "Rejected", tier="N").__dict__, "tier": "N"},
            ],
        }

        html = render_html(digest)

        self.assertIn("Core", html)
        self.assertIn("Secondary", html)
        self.assertNotIn("Rejected", html)
        self.assertIn("replica methods; many-body learning", html)


if __name__ == "__main__":
    unittest.main()
