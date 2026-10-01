from __future__ import annotations

import os
import stat
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from arxivmail.arxiv import fetch_recent_api, parse_atom, parse_rss, split_version
from arxivmail.config import load_config
from arxivmail.models import Assessment, LocalScore, Paper, RankedPaper, ZoteroItem
from arxivmail.pipeline import (
    _dedupe_papers,
    _message_fingerprint,
    _subject,
    _write_outputs,
    calibrate,
    deliver_reserved_digest,
    generate_digest,
)
from arxivmail.ranking import _concise_why_match, lexical_score
from arxivmail.secrets import Credentials
from arxivmail.select import select_digest_papers
from arxivmail.state import StateStore


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "interests.toml"


def paper(identifier: str, title: str, abstract: str, *, announce_type: str = "new") -> Paper:
    base, version = split_version(identifier)
    now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    return Paper(
        arxiv_id=f"{base}v{version}",
        base_id=base,
        version=version,
        title=title,
        authors=("A. Researcher",),
        abstract=abstract,
        categories=("cond-mat.dis-nn",),
        primary_category="cond-mat.dis-nn",
        published=now,
        updated=now,
        announce_type=announce_type,
    )


def assessment(identifier: str, tier: str, score: int) -> Assessment:
    return Assessment(
        arxiv_id=identifier,
        tier=tier,
        score=score,
        confidence=0.9,
        fit_type="physics_of_learning",
        matched_clusters=("statistical_physics_of_learning",),
        monthly_focus_hits=(),
        why_match="A concrete theoretical match.",
        summary_sentences=("One.", "Two.", "Three."),
        reading_value="Read for the method.",
    )


class CoreTests(unittest.TestCase):
    def test_config_encodes_complete_profile_and_golden_labels(self) -> None:
        config = load_config(CONFIG)
        self.assertEqual(len(config.clusters), 14)
        self.assertEqual("".join(item.tier for item in config.calibration), "MMMPMMPP")
        focus = config.focus_for(datetime(2026, 9, 6).date())
        self.assertIsNotNone(focus)
        assert focus is not None
        self.assertIn("replica", focus.description.casefold())

    def test_hidden_replica_and_associative_memory_methods_survive_prefilter(self) -> None:
        config = load_config(CONFIG)
        focus = config.focus_for(datetime(2026, 9, 6).date())
        linear_regression = paper(
            "2609.03111v1",
            "Large deviations for linear regressions",
            "We derive the rate function with a zero-temperature replica calculation in a high-dimensional teacher-student model.",
        )
        neuromodulation = paper(
            "2512.13859v1",
            "Neuromodulation-inspired gated associative memory networks",
            "Many-body simulations and dynamical mean-field theory reveal a spin-glass transition and Hopfield storage capacity.",
        )
        for item in (linear_regression, neuromodulation):
            with self.subTest(arxiv_id=item.arxiv_id):
                score = lexical_score(item, config, focus)
                self.assertGreater(score.lexical, 0.25)
                self.assertTrue(score.focus_hits)

    def test_why_match_is_bounded_to_one_concrete_line(self) -> None:
        value = _concise_why_match(
            "It studies SK complexity. It uses one-step replica symmetry breaking. "
            "A third sentence should not appear."
        )
        self.assertEqual(
            value,
            "It studies SK complexity; it uses one-step replica symmetry breaking.",
        )

    def test_rss_parser_uses_versioned_guid_and_announce_type(self) -> None:
        raw = b"""<?xml version='1.0'?>
        <rss xmlns:arxiv='http://arxiv.org/schemas/atom' xmlns:dc='http://purl.org/dc/elements/1.1/'>
          <channel><item>
            <title> A revised &amp; useful paper </title>
            <guid>oai:arXiv.org:2609.12345v2</guid>
            <dc:creator>Ada Lovelace, Emmy Noether</dc:creator>
            <pubDate>Sun, 06 Sep 2026 04:00:00 +0000</pubDate>
            <category>cs.LG</category><arxiv:primary_category>cond-mat.dis-nn</arxiv:primary_category>
            <arxiv:announce_type>replace</arxiv:announce_type>
            <description><![CDATA[<p>Abstract: Replica theory finds a transition.</p>]]></description>
          </item></channel>
        </rss>"""
        parsed = parse_rss(raw)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].base_id, "2609.12345")
        self.assertEqual(parsed[0].version, 2)
        self.assertEqual(parsed[0].announce_type, "replace")
        self.assertEqual(parsed[0].abstract, "Replica theory finds a transition.")
        self.assertEqual(parsed[0].categories, ("cond-mat.dis-nn", "cs.LG"))

    def test_atom_parser_deduplicates_to_highest_version(self) -> None:
        raw = b"""<?xml version='1.0'?>
        <feed xmlns='http://www.w3.org/2005/Atom' xmlns:arxiv='http://arxiv.org/schemas/atom'>
          <entry><id>http://arxiv.org/abs/2609.12345v1</id><updated>2026-09-01T00:00:00Z</updated><published>2026-09-01T00:00:00Z</published><title>Old</title><summary>Old abstract.</summary><author><name>A</name></author><category term='cs.LG'/><arxiv:primary_category term='cs.LG'/></entry>
          <entry><id>http://arxiv.org/abs/2609.12345v2</id><updated>2026-09-02T00:00:00Z</updated><published>2026-09-01T00:00:00Z</published><title>New</title><summary>New abstract.</summary><author><name>A</name></author><category term='cs.LG'/><arxiv:primary_category term='cs.LG'/></entry>
        </feed>"""
        parsed = parse_atom(raw)
        self.assertEqual([(item.arxiv_id, item.title) for item in parsed], [("2609.12345v2", "New")])

    def test_atom_parser_preserves_old_style_id_from_alternate_link(self) -> None:
        raw = b"""<?xml version='1.0'?>
        <feed xmlns='http://www.w3.org/2005/Atom' xmlns:arxiv='http://arxiv.org/schemas/atom'>
          <entry><id>http://arxiv.org/abs/cond-mat/0207270</id><link rel='alternate' href='https://arxiv.org/abs/cond-mat/0207270v3'/><updated>2026-09-02T00:00:00Z</updated><published>2002-07-01T00:00:00Z</published><title>Old style</title><summary>Abstract.</summary><author><name>A</name></author><category term='cond-mat'/><arxiv:primary_category term='cond-mat'/></entry>
        </feed>"""
        parsed = parse_atom(raw)
        self.assertEqual(parsed[0].base_id, "cond-mat/0207270")
        self.assertEqual(parsed[0].version, 3)

    def test_api_recovery_fails_closed_before_truncating(self) -> None:
        raw = b"""<?xml version='1.0'?>
        <feed xmlns='http://www.w3.org/2005/Atom'
              xmlns:opensearch='http://a9.com/-/spec/opensearch/1.1/'>
          <opensearch:totalResults>2001</opensearch:totalResults>
        </feed>"""
        with patch("arxivmail.arxiv.request_bytes", return_value=(raw, {})):
            with self.assertRaisesRegex(RuntimeError, "exceeding the 2000 result cap"):
                fetch_recent_api(
                    ("cs.LG",),
                    datetime(2026, 9, 1, tzinfo=timezone.utc),
                    datetime(2026, 9, 6, tzinfo=timezone.utc),
                    max_results=2000,
                )

    def test_recovery_merge_keeps_authoritative_rss_announcement(self) -> None:
        rss = paper("2609.12345v2", "RSS title", "RSS abstract.", announce_type="cross")
        api = Paper(
            arxiv_id=rss.arxiv_id,
            base_id=rss.base_id,
            version=rss.version,
            title="API title",
            authors=rss.authors,
            abstract="API abstract.",
            categories=("cs.LG",),
            primary_category="cs.LG",
            published=rss.published,
            updated=rss.updated,
            announce_type="replace",
        )
        merged = _dedupe_papers([rss, api])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].title, "RSS title")
        self.assertEqual(merged[0].announce_type, "cross")
        self.assertEqual(merged[0].categories, ("cond-mat.dis-nn", "cs.LG"))

    def test_selector_never_pads_and_caps_less_urgent(self) -> None:
        config = load_config(CONFIG)
        inputs = []
        tiers = (("M", 93), ("M", 82), ("P", 75), ("P", 70), ("P", 65), ("P", 60), ("N", 99))
        for index, (tier, score) in enumerate(tiers):
            item = paper(f"2609.{index:05d}v1", f"Paper {index}", "Replica method and learning.")
            inputs.append((item, LocalScore(combined=0.8), assessment(item.arxiv_id, tier, score)))
        main, less, revisions = select_digest_papers(inputs, set(), config)
        self.assertEqual(len(main), 2)
        self.assertEqual(len(less), 3)
        self.assertFalse(revisions)
        self.assertTrue(all(item.assessment.tier != "N" for item in (*main, *less)))

    def test_state_only_reopens_material_higher_versions(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "state.json"
            state = StateStore(path)
            first = paper("2609.12345v1", "Title", "The same abstract.")
            digest_item = RankedPaper(first, assessment(first.arxiv_id, "M", 90))
            from arxivmail.models import Digest

            digest = Digest(
                digest_date=datetime(2026, 9, 6).date(),
                run_id="one",
                monthly_focus="focus",
                screened_count=1,
                main=(digest_item,),
                less_urgent=(),
            )
            state.record_run(digest, sent=True)
            state.save()
            loaded = StateStore(path)
            unchanged = paper("2609.12345v2", "Title", "The same abstract.", announce_type="replace")
            changed = paper(
                "2609.12345v2",
                "Title",
                "A substantially different abstract with a new theorem.",
                announce_type="replace",
            )
            self.assertEqual(loaded.eligibility(unchanged), (False, False))
            self.assertEqual(loaded.eligibility(changed), (True, True))
            unseen_revision = paper("2609.99999v4", "Unseen", "An abstract.", announce_type="replace")
            self.assertEqual(loaded.eligibility(unseen_revision), (True, False))

    def test_empty_completed_run_advances_recovery_without_counting_as_sent(self) -> None:
        from arxivmail.models import Digest

        with TemporaryDirectory() as tmp_dir:
            state = StateStore(Path(tmp_dir) / "state.json")
            digest = Digest(
                digest_date=datetime(2026, 9, 6).date(),
                run_id="empty",
                monthly_focus="focus",
                screened_count=200,
                main=(),
                less_urgent=(),
            )
            state.record_run(digest, sent=False)
            self.assertIsNotNone(state.last_completed_run)
            self.assertIsNone(state.last_successful_send)

    def test_reserved_digest_is_suppressed_then_marked_delivered(self) -> None:
        from arxivmail.models import Digest

        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state_path = root / "state.json"
            output_path = root / "output"
            first = paper("2609.12345v1", "A relevant paper", "Replica theory of learning.")
            scored = LocalScore(combined=0.9)
            judged = assessment(first.arxiv_id, "M", 92)
            ranked = RankedPaper(first, judged, scored)
            digest = Digest(
                digest_date=datetime(2026, 9, 6).date(),
                run_id="reserved-run",
                monthly_focus="focus",
                screened_count=1,
                main=(ranked,),
                less_urgent=(),
            )
            html, text = _write_outputs(digest, output_path, [(first, scored, judged)])
            config = load_config(CONFIG)
            sender = str(config.delivery["sender"])
            recipient = str(config.delivery["recipient"])
            fingerprint = _message_fingerprint(
                run_id=digest.run_id,
                sender=sender,
                recipient=recipient,
                subject=_subject(digest),
                text=text,
                html=html,
            )
            state = StateStore(state_path)
            state.record_run(
                digest,
                sent=False,
                reserved=True,
                message_sha256=fingerprint,
            )
            state.save()
            self.assertEqual(state.eligibility(first), (False, False))
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((output_path / "digest-data.json").stat().st_mode), 0o600)

            credentials = Credentials("openai", "", "gmail")
            with (
                patch(
                    "arxivmail.pipeline.load_gmail_app_password",
                    return_value=credentials.gmail_app_password,
                ),
                patch("arxivmail.pipeline.send_gmail") as send,
            ):
                delivered, selected = deliver_reserved_digest(
                    config_path=CONFIG,
                    state_path=state_path,
                    output_dir=output_path,
                )
            self.assertTrue(delivered)
            self.assertEqual(selected, 1)
            send.assert_called_once()
            delivered_state = StateStore(state_path)
            self.assertEqual(delivered_state.run(digest.run_id)["status"], "delivered")
            self.assertTrue(delivered_state.run(digest.run_id)["sent"])

    def test_reservation_rejects_cache_from_a_different_zotero_account(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state_path = root / "state.json"
            state = StateStore(state_path)
            state.update_zotero(
                [
                    ZoteroItem(
                        key="OLD",
                        title="Private old-account title",
                        abstract="Old account abstract",
                        tags=(),
                        item_type="journalArticle",
                        date_modified="2026-09-01",
                    )
                ],
                library_version=10,
                user_id="old-user",
            )
            state.save()
            zotero = Mock()
            zotero.identity.return_value = ("new-user", 20)
            zotero.sync.side_effect = RuntimeError("sync failed")
            credentials = Credentials("openai", "zotero", "")
            with (
                patch("arxivmail.pipeline.Credentials.load", return_value=credentials),
                patch("arxivmail.pipeline._fetch_papers", return_value=([], "")),
                patch("arxivmail.pipeline.ZoteroClient", return_value=zotero),
            ):
                with self.assertRaisesRegex(RuntimeError, "matching cached profile"):
                    generate_digest(
                        config_path=CONFIG,
                        state_path=state_path,
                        output_dir=root / "output",
                        reserve=True,
                    )

    def test_calibration_never_falls_back_to_unverified_zotero_cache(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            state_path = root / "state.json"
            state = StateStore(state_path)
            state.update_zotero(
                [
                    ZoteroItem(
                        key="OLD",
                        title="Private old-account title",
                        abstract="Old account abstract",
                        tags=(),
                        item_type="journalArticle",
                        date_modified="2026-09-01",
                    )
                ],
                library_version=10,
                user_id="old-user",
            )
            state.save()
            zotero = Mock()
            zotero.identity.return_value = ("new-user", 20)
            zotero.sync.side_effect = RuntimeError("sync failed")
            captured: dict[str, list[ZoteroItem]] = {}

            def fake_shortlist(papers, zotero_items, config, focus, client):
                captured["zotero"] = list(zotero_items)
                return []

            credentials = Credentials("openai", "zotero", "")
            with (
                patch("arxivmail.pipeline.Credentials.load", return_value=credentials),
                patch(
                    "arxivmail.pipeline.fetch_by_ids",
                    return_value=[paper("2609.04006v1", "SK", "Replica theory.")],
                ),
                patch("arxivmail.pipeline.ZoteroClient", return_value=zotero),
                patch("arxivmail.pipeline.shortlist", side_effect=fake_shortlist),
            ):
                calibrate(
                    config_path=CONFIG,
                    state_path=state_path,
                    output_dir=root / "output",
                )
            self.assertEqual(captured["zotero"], [])

    def test_secret_loader_rejects_world_readable_file(self) -> None:
        with TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=False):
            for name in ("OPENAI_API_KEY", "ZOTERO_API_KEY", "GMAIL_APP_PASSWORD"):
                os.environ.pop(name, None)
            path = Path(tmp_dir) / "secrets"
            path.write_text("openai\nzotero\ngm\u00a0ail\n")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
            with self.assertRaises(PermissionError):
                Credentials.load(path)
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            loaded = Credentials.load(path, require_gmail=True)
            self.assertEqual(loaded.openai_api_key, "openai")
            self.assertEqual(loaded.gmail_app_password, "gmail")
            self.assertNotIn("openai", repr(loaded))

    def test_secret_loader_rejects_ambiguous_two_line_legacy_file(self) -> None:
        with TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=False):
            for name in ("OPENAI_API_KEY", "ZOTERO_API_KEY", "GMAIL_APP_PASSWORD"):
                os.environ.pop(name, None)
            path = Path(tmp_dir) / "secrets"
            path.write_text("openai\npossibly-gmail\n")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                Credentials.load(path)

    def test_secret_loader_supports_unambiguous_named_entries(self) -> None:
        with TemporaryDirectory() as tmp_dir, patch.dict(os.environ, {}, clear=False):
            for name in ("OPENAI_API_KEY", "ZOTERO_API_KEY", "GMAIL_APP_PASSWORD"):
                os.environ.pop(name, None)
            path = Path(tmp_dir) / "secrets"
            path.write_text("OPENAI_API_KEY=openai\nGMAIL_APP_PASSWORD=gm ail\n")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            loaded = Credentials.load(path, require_gmail=True)
            self.assertEqual(loaded.gmail_app_password, "gmail")


if __name__ == "__main__":
    unittest.main()
