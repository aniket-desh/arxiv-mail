from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_SECRET_FILE = Path.home() / ".arxiv"
SECRET_FIELDS = {
    "OPENAI_API_KEY": "openai",
    "ZOTERO_API_KEY": "zotero",
    "GMAIL_APP_PASSWORD": "gmail",
}


@dataclass(frozen=True, slots=True)
class Credentials:
    openai_api_key: str = field(repr=False)
    zotero_api_key: str = field(default="", repr=False)
    gmail_app_password: str = field(default="", repr=False)

    @classmethod
    def load(
        cls,
        path: str | Path = DEFAULT_SECRET_FILE,
        *,
        require_gmail: bool = False,
    ) -> "Credentials":
        values = {
            "openai": os.environ.get("OPENAI_API_KEY", "").strip(),
            "zotero": os.environ.get("ZOTERO_API_KEY", "").strip(),
            "gmail": _compact_password(os.environ.get("GMAIL_APP_PASSWORD", "")),
        }
        if not all(values.values()):
            secret_path = Path(path).expanduser()
            if secret_path.exists():
                _assert_private(secret_path)
                file_values = _read_secret_values(secret_path)
                for name, value in file_values.items():
                    if not values[name]:
                        values[name] = _compact_password(value) if name == "gmail" else value
        if not values["openai"]:
            raise ValueError("OPENAI_API_KEY is missing (or first non-empty ~/.arxiv entry)")
        if require_gmail and not values["gmail"]:
            raise ValueError("GMAIL_APP_PASSWORD is required when --send is used")
        return cls(
            openai_api_key=values["openai"],
            zotero_api_key=values["zotero"],
            gmail_app_password=values["gmail"],
        )


def load_gmail_app_password(path: str | Path = DEFAULT_SECRET_FILE) -> str:
    value = _compact_password(os.environ.get("GMAIL_APP_PASSWORD", ""))
    if not value:
        secret_path = Path(path).expanduser()
        if secret_path.exists():
            _assert_private(secret_path)
            value = _compact_password(_read_secret_values(secret_path).get("gmail", ""))
    if not value:
        raise ValueError("GMAIL_APP_PASSWORD is required for delivery")
    return value


def _compact_password(value: str) -> str:
    return "".join(value.split())


def _read_secret_values(path: Path) -> dict[str, str]:
    entries = [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    named: dict[str, str] = {}
    positional: list[str] = []
    for entry in entries:
        if "=" not in entry:
            positional.append(entry)
            continue
        key, value = entry.split("=", 1)
        field = SECRET_FIELDS.get(key.strip())
        if field is None:
            raise ValueError(f"Unknown credential name in {path}: {key.strip()}")
        named[field] = value.strip()
    if named and positional:
        raise ValueError(f"Do not mix named and positional credentials in {path}")
    if named:
        return named
    if len(positional) == 2:
        raise ValueError(
            f"Two positional credentials in {path} are ambiguous; use OPENAI_API_KEY=, "
            "ZOTERO_API_KEY=, and GMAIL_APP_PASSWORD= names"
        )
    return dict(zip(("openai", "zotero", "gmail"), positional, strict=False))


def _assert_private(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"{path} must not be accessible by group or others; run chmod 600")
