from __future__ import annotations

import argparse
import json
from pathlib import Path

from .arxiv import fetch_daily_rss
from .config import DEFAULT_CONFIG, load_config
from .mailer import check_gmail_login
from .openai_api import OpenAIClient
from .pipeline import calibrate, deliver_reserved_digest, generate_digest
from .secrets import Credentials, DEFAULT_SECRET_FILE
from .state import StateStore
from .zotero import ZoteroClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arxivmail", description="Personalized arXiv reading digest")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--state", default="state/state.json")
    parser.add_argument("--output", default="output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="build a non-sending digest")
    generate.add_argument(
        "--reserve",
        action="store_true",
        help="reserve selected papers in state before a separate delivery step",
    )
    generate.add_argument("--source", choices=("rss", "api"), default="rss")
    generate.add_argument("--lookback-hours", type=int)

    subparsers.add_parser("calibrate", help="run the eight-paper golden taste check")
    subparsers.add_parser("deliver", help="send a previously reserved rendered digest")
    doctor = subparsers.add_parser("doctor", help="validate configuration and credentials")
    doctor.add_argument("--online", action="store_true", help="also make small read-only API checks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        digest = generate_digest(
            config_path=args.config,
            state_path=args.state,
            output_dir=args.output,
            reserve=args.reserve,
            source=args.source,
            lookback_hours=args.lookback_hours,
        )
        action = "reserved" if args.reserve and digest.selected_count else "rendered"
        print(
            f"Digest {action}: {digest.screened_count} screened, "
            f"{len(digest.main)} read first, {len(digest.less_urgent)} less urgent, "
            f"{len(digest.revisions)} revisions"
        )
        return 0
    if args.command == "deliver":
        delivered, selected_count = deliver_reserved_digest(
            config_path=args.config,
            state_path=args.state,
            output_dir=args.output,
        )
        if delivered:
            print(f"Digest delivered: {selected_count} paper{'s' if selected_count != 1 else ''}")
        else:
            print("Digest delivery skipped: no papers cleared the threshold")
        return 0
    if args.command == "calibrate":
        _, results = calibrate(
            config_path=args.config,
            state_path=args.state,
            output_dir=args.output,
        )
        for result in results:
            symbol = "✓" if result["match"] else "✗"
            print(
                f"{symbol} {result['arxiv_id']}: expected {result['expected']}, "
                f"got {result['actual']} ({result['score']})"
            )
        matches = sum(bool(item["match"]) for item in results)
        print(f"Calibration: {matches}/{len(results)} exact labels")
        return 0 if matches == len(results) else 2
    return _doctor(args.config, args.state, online=args.online)


def _doctor(config_path: str, state_path: str, *, online: bool) -> int:
    config = load_config(config_path)
    credentials = Credentials.load()
    StateStore(state_path)
    print(f"Config: {len(config.clusters)} clusters, {len(config.calibration)} calibration papers")
    print(f"Secrets: private file {DEFAULT_SECRET_FILE}; values not displayed")
    if not online:
        return 0
    rss_count = len(fetch_daily_rss(config.categories))
    print(f"arXiv RSS: {rss_count} unique announcements")
    if credentials.zotero_api_key:
        user_id, _ = ZoteroClient(credentials.zotero_api_key).identity()
        print(f"Zotero: read access confirmed for user {user_id}")
    if credentials.gmail_app_password:
        check_gmail_login(
            sender=str(config.delivery.get("sender") or config.delivery["recipient"]),
            app_password=credentials.gmail_app_password,
        )
        print("Gmail SMTP: App Password authentication confirmed; no message sent")
    vectors = OpenAIClient(credentials.openai_api_key).embeddings(
        ["statistical mechanics of learning"], model=str(config.models["embedding"])
    )
    print(f"OpenAI embeddings: {len(vectors[0])} dimensions")
    return 0
