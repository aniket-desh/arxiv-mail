from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "interests.toml"


@dataclass(frozen=True, slots=True)
class Cluster:
    id: str
    name: str
    weight: float
    phrases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Focus:
    id: str
    label: str
    starts: date
    expires: date
    weight: float
    phrases: tuple[str, ...]
    description: str
    cluster_boosts: dict[str, float]

    def active_on(self, day: date) -> bool:
        return self.starts <= day <= self.expires


@dataclass(frozen=True, slots=True)
class Calibration:
    arxiv_id: str
    tier: str
    reason: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    profile: dict[str, Any]
    retrieval: dict[str, Any]
    selection: dict[str, Any]
    models: dict[str, Any]
    delivery: dict[str, Any]
    ranking: dict[str, Any]
    quantum_direction: dict[str, Any]
    bridges: dict[str, Any]
    clusters: tuple[Cluster, ...]
    monthly_focus: tuple[Focus, ...]
    calibration: tuple[Calibration, ...]

    def focus_for(self, day: date) -> Focus | None:
        return next((focus for focus in self.monthly_focus if focus.active_on(day)), None)

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(self.retrieval.get("categories", ()))


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_config(path: str | Path = DEFAULT_CONFIG) -> AppConfig:
    config_path = Path(path).expanduser()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    clusters = tuple(
        Cluster(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            weight=float(item.get("weight", 1.0)),
            phrases=tuple(str(term) for term in item.get("phrases", ())),
        )
        for item in raw.get("clusters", ())
    )
    focuses_list: list[Focus] = []
    for item in raw.get("monthly_focus", ()):
        focus_id = str(item.get("id") or item.get("month"))
        label = str(item.get("label") or item.get("name") or focus_id)
        default_start = f"{focus_id}-01" if len(focus_id) == 7 else focus_id
        focuses_list.append(
            Focus(
                id=focus_id,
                label=label,
                starts=_parse_date(item.get("starts", default_start)),
                expires=_parse_date(item.get("expires", item.get("expires_after"))),
                weight=float(item.get("weight", 2.0)),
                phrases=tuple(str(term) for term in item.get("phrases", ())),
                description=str(item.get("description", label)),
                cluster_boosts={
                    str(key): float(value) for key, value in item.get("cluster_boosts", {}).items()
                },
            )
        )
    focuses = tuple(focuses_list)
    calibrations = tuple(
        Calibration(
            arxiv_id=str(item["arxiv_id"]),
            tier=str(item.get("tier") or item.get("label")),
            reason=str(item["reason"]),
        )
        for item in raw.get("calibration", ())
    )
    if not clusters:
        raise ValueError(f"No interest clusters found in {config_path}")
    return AppConfig(
        profile=dict(raw.get("profile", {})),
        retrieval=dict(raw.get("retrieval", {})),
        selection=dict(raw.get("selection", {})),
        models=dict(raw.get("models", {})),
        delivery=dict(raw.get("delivery", {})),
        ranking=dict(raw.get("ranking", {})),
        quantum_direction=dict(raw.get("quantum_direction", {})),
        bridges=dict(raw.get("bridges", {})),
        clusters=clusters,
        monthly_focus=focuses,
        calibration=calibrations,
    )
