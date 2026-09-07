"""Metric registry for baselines.

Extensible without migrations: derived metrics (velocity, acceleration,
relative_performance) live here as registry entries, not as TextChoices.
Add a new metric by registering its spec — no schema/code-enum change.

Raw metrics map to EngagementSnapshot columns; derived metrics have
no Snapshot column and are computed by services.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    name: str
    description: str
    is_derived: bool = False
    unit: str = "count"


_REGISTRY: dict[str, MetricSpec] = {}


def register(spec: MetricSpec) -> MetricSpec:
    _REGISTRY[spec.name] = spec
    return spec


# Raw snapshot metrics (columns on EngagementSnapshot).
register(MetricSpec("views", "Post views"))
register(MetricSpec("forwards", "Telegram forwards"))
register(MetricSpec("shares", "Shares / reposts"))
register(MetricSpec("reactions", "Reactions / likes"))
register(MetricSpec("replies", "Replies / comments"))
register(MetricSpec("saves", "Bookmarks / saves"))
# Derived metrics — no column, computed from snapshots.
register(MetricSpec("view_velocity", "Views per hour in bucket", is_derived=True, unit="count/h"))
register(MetricSpec("share_velocity", "Shares per hour in bucket", is_derived=True, unit="count/h"))
register(
    MetricSpec("forward_velocity", "Forwards per hour in bucket", is_derived=True, unit="count/h")
)
register(
    MetricSpec(
        "engagement_velocity",
        "Aggregate engagement delta per hour",
        is_derived=True,
        unit="count/h",
    )
)
register(
    MetricSpec(
        "engagement_acceleration",
        "Change in engagement velocity",
        is_derived=True,
        unit="count/h^2",
    )
)
register(
    MetricSpec(
        "relative_performance",
        "Score relative to source baseline (percentile or z)",
        is_derived=True,
        unit="0..1",
    )
)


def all_metric_names(*, include_derived: bool = True) -> list[str]:
    return [spec.name for spec in _REGISTRY.values() if include_derived or not spec.is_derived]


def raw_metric_names() -> list[str]:
    return all_metric_names(include_derived=False)


def derived_metric_names() -> list[str]:
    return [spec.name for spec in _REGISTRY.values() if spec.is_derived]


def is_valid_metric(name: str) -> bool:
    return name in _REGISTRY


def is_derived_metric(name: str) -> bool:
    spec = _REGISTRY.get(name)
    return spec.is_derived if spec else False


def get_spec(name: str) -> MetricSpec | None:
    return _REGISTRY.get(name)


def metric_choices() -> list[tuple[str, str]]:
    """For admin/form display when needed. Not the source of truth for validation."""
    return [(spec.name, spec.description) for spec in _REGISTRY.values()]
