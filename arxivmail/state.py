from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .models import Digest, Paper, ZoteroItem


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "sent": {},
            "runs": [],
            "zotero": {"library_version": 0, "items": {}},
        }
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            if loaded.get("schema_version") != 1:
                raise ValueError("Unsupported state schema")
            self.data.update(loaded)

    @property
    def last_successful_send(self) -> datetime | None:
        for run in reversed(self.data.get("runs", [])):
            if run.get("sent"):
                return datetime.fromisoformat(run["completed_at"])
        return None

    @property
    def last_completed_run(self) -> datetime | None:
        for run in reversed(self.data.get("runs", [])):
            if run.get("completed_at"):
                return datetime.fromisoformat(run["completed_at"])
        return None

    def eligibility(self, paper: Paper) -> tuple[bool, bool]:
        previous = self.data.get("sent", {}).get(paper.base_id)
        if previous is None:
            return True, False
        if paper.version <= int(previous.get("version", 0)):
            return False, False
        old_abstract = previous.get("abstract", "")
        abstract_ratio = SequenceMatcher(None, old_abstract, paper.abstract).ratio() if old_abstract else 0.0
        title_changed = previous.get("title", "") != paper.title
        old_comments = previous.get("comments", "")
        comments_changed = bool(old_comments and paper.comments and old_comments != paper.comments)
        material = title_changed or abstract_ratio < 0.94 or comments_changed
        return material, material

    def zotero_items(self) -> list[ZoteroItem]:
        items = self.data.get("zotero", {}).get("items", {})
        return [
            ZoteroItem(
                key=key,
                title=item.get("title", ""),
                abstract=item.get("abstract", ""),
                tags=tuple(item.get("tags", ())),
                item_type=item.get("item_type", ""),
                date_modified=item.get("date_modified", ""),
                creators=tuple(item.get("creators", ())),
                collections=tuple(item.get("collections", ())),
                collection_keys=tuple(item.get("collection_keys", ())),
            )
            for key, item in items.items()
        ]

    def update_zotero(
        self,
        items: list[ZoteroItem],
        *,
        library_version: int,
        user_id: str,
    ) -> None:
        self.data["zotero"] = {
            "user_id": user_id,
            "library_version": library_version,
            "items": {item.key: asdict(item) for item in items},
        }

    def record_run(
        self,
        digest: Digest,
        *,
        sent: bool,
        reserved: bool = False,
        message_sha256: str = "",
    ) -> None:
        if sent and reserved:
            raise ValueError("A run cannot be both newly reserved and already delivered")
        completed_at = datetime.now(timezone.utc).isoformat()
        status = "delivered" if sent else "reserved" if reserved and digest.selected_count else "empty"
        self.data.setdefault("runs", []).append(
            {
                "run_id": digest.run_id,
                "date": digest.digest_date.isoformat(),
                "completed_at": completed_at,
                "sent": sent,
                "status": status,
                "selected": digest.selected_count,
                "items": [ranked.paper.arxiv_id for ranked in digest.all_ranked],
                "message_sha256": message_sha256,
            }
        )
        self.data["runs"] = self.data["runs"][-90:]
        if sent or reserved:
            sent_map = self.data.setdefault("sent", {})
            for ranked in digest.all_ranked:
                paper = ranked.paper
                sent_map[paper.base_id] = {
                    "version": paper.version,
                    "title": paper.title,
                    "abstract": paper.abstract,
                    "abstract_hash": hashlib.sha256(paper.abstract.encode()).hexdigest(),
                    "comments": paper.comments,
                    "updated": paper.updated.isoformat(),
                    "last_sent": completed_at,
                    "tier": ranked.assessment.tier,
                    "status": status,
                    "run_id": digest.run_id,
                }

    def run(self, run_id: str) -> dict[str, Any] | None:
        return next(
            (run for run in reversed(self.data.get("runs", [])) if run.get("run_id") == run_id),
            None,
        )

    def mark_run_delivered(self, run_id: str) -> None:
        run = self.run(run_id)
        if run is None:
            raise ValueError(f"Unknown reserved run: {run_id}")
        if run.get("status") == "delivered" or run.get("sent"):
            raise ValueError(f"Run {run_id} was already marked delivered")
        if run.get("status") != "reserved":
            raise ValueError(f"Run {run_id} is not awaiting delivery")
        delivered_at = datetime.now(timezone.utc).isoformat()
        run["status"] = "delivered"
        run["sent"] = True
        run["delivered_at"] = delivered_at
        for item in self.data.get("sent", {}).values():
            if item.get("run_id") == run_id:
                item["status"] = "delivered"
                item["last_sent"] = delivered_at

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        os.replace(temp_path, self.path)
        self.path.chmod(0o600)
