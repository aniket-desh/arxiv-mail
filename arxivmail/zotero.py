from __future__ import annotations

import urllib.parse
from dataclasses import replace
from typing import Any

from .http import request_json
from .models import ZoteroItem
from .state import StateStore


BASE_URL = "https://api.zotero.org"
SKIP_TYPES = {"attachment", "note", "annotation", "deleted"}


class ZoteroClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.headers = {
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": "3",
            "Accept": "application/json",
        }

    def identity(self) -> tuple[str, int]:
        payload, _ = request_json(f"{BASE_URL}/keys/current", headers=self.headers)
        user_id = str(payload.get("userID", ""))
        access = payload.get("access", {}).get("user", {})
        if not user_id or not access.get("library"):
            raise PermissionError("The Zotero API key does not grant read access to the user library")
        return user_id, int(payload.get("lastModifiedVersion", 0) or 0)

    def sync(
        self,
        state: StateStore,
        *,
        max_items: int = 500,
        identity: tuple[str, int] | None = None,
    ) -> list[ZoteroItem]:
        user_id, identity_version = identity or self.identity()
        cached = {item.key: item for item in state.zotero_items()}
        state_zotero = state.data.get("zotero", {})
        previous_version = int(state_zotero.get("library_version", 0) or 0)
        same_user = str(state_zotero.get("user_id", "")) == user_id
        if any(item.collections and not item.collection_keys for item in cached.values()):
            same_user = False

        collections = self._collection_names(user_id)
        cached = {
            key: replace(
                item,
                collections=tuple(collections.get(value, value) for value in item.collection_keys),
            )
            if item.collection_keys
            else item
            for key, item in cached.items()
        }
        if cached and previous_version and same_user:
            changed, terminal_version = self._versions(user_id, since=previous_version)
            deleted = self._deleted(user_id, since=previous_version)
            for key in deleted:
                cached.pop(key, None)
            fetched = self._fetch_item_keys(user_id, list(changed), collections)
            fetched_keys = {item.key for item in fetched}
            for key in set(changed) - fetched_keys:
                cached.pop(key, None)
            for item in fetched:
                if item.item_type in SKIP_TYPES:
                    cached.pop(item.key, None)
                else:
                    cached[item.key] = item
            library_version = max(identity_version, terminal_version, previous_version)
        else:
            versions, terminal_version = self._versions(user_id)
            keys = list(versions)
            cached = {
                item.key: item
                for item in self._fetch_item_keys(user_id, keys, collections)
                if item.item_type not in SKIP_TYPES
            }
            library_version = max(identity_version, terminal_version)

        items = sorted(cached.values(), key=lambda item: item.date_modified, reverse=True)
        retained = items[:max_items]
        state.update_zotero(retained, library_version=library_version, user_id=user_id)
        return retained

    def _versions(self, user_id: str, since: int | None = None) -> tuple[dict[str, int], int]:
        params: dict[str, Any] = {"format": "versions", "includeTrashed": 1}
        if since:
            params["since"] = since
        url = f"{BASE_URL}/users/{user_id}/items/top?{urllib.parse.urlencode(params)}"
        payload, headers = request_json(url, headers=self.headers)
        terminal = int(headers.get("Last-Modified-Version", 0) or 0)
        return {str(key): int(value) for key, value in payload.items()}, terminal

    def _fetch_item_keys(
        self,
        user_id: str,
        keys: list[str],
        collections: dict[str, str],
    ) -> list[ZoteroItem]:
        results: list[ZoteroItem] = []
        for start in range(0, len(keys), 50):
            batch = keys[start : start + 50]
            params = urllib.parse.urlencode(
                {"itemKey": ",".join(batch), "limit": 50, "includeTrashed": 1}
            )
            payload, _ = request_json(
                f"{BASE_URL}/users/{user_id}/items?{params}", headers=self.headers
            )
            for wrapper in payload:
                data = wrapper.get("data", {})
                deleted = bool(data.get("deleted"))
                creator_names = []
                for creator in data.get("creators", []):
                    name = creator.get("name") or " ".join(
                        part for part in (creator.get("firstName"), creator.get("lastName")) if part
                    )
                    if name:
                        creator_names.append(name)
                results.append(
                    ZoteroItem(
                        key=str(data.get("key") or wrapper.get("key") or ""),
                        title=str(data.get("title", "")),
                        abstract=str(data.get("abstractNote", "")),
                        tags=tuple(str(tag.get("tag", "")) for tag in data.get("tags", []) if tag.get("tag")),
                        item_type="deleted" if deleted else str(data.get("itemType", "")),
                        date_modified=str(data.get("dateModified", "")),
                        creators=tuple(creator_names),
                        collections=tuple(
                            collections.get(key, key) for key in data.get("collections", [])
                        ),
                        collection_keys=tuple(str(key) for key in data.get("collections", [])),
                    )
                )
        return results

    def _collection_names(self, user_id: str) -> dict[str, str]:
        results: dict[str, str] = {}
        start = 0
        while True:
            params = urllib.parse.urlencode({"limit": 100, "start": start})
            payload, headers = request_json(
                f"{BASE_URL}/users/{user_id}/collections?{params}", headers=self.headers
            )
            for wrapper in payload:
                data = wrapper.get("data", {})
                results[str(data.get("key") or wrapper.get("key"))] = str(data.get("name", ""))
            total = int(headers.get("Total-Results", len(results)) or len(results))
            start += len(payload)
            if not payload or start >= total:
                break
        return results

    def _deleted(self, user_id: str, since: int) -> set[str]:
        payload, _ = request_json(
            f"{BASE_URL}/users/{user_id}/deleted?{urllib.parse.urlencode({'since': since})}",
            headers=self.headers,
        )
        return {str(key) for key in payload.get("items", [])}
