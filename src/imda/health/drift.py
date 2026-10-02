"""Structural drift detection: compare an adapter fingerprint to a recorded baseline.

A baseline is a fingerprint plus an optional ``"_ignore"`` list of keys (dotted paths for
nested keys) that vary by nature, such as row counts. Nested dicts are flattened to dotted
paths. Lists are compared whole and in order. A missing baseline is not drift: it is the
first run, and the report says so.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

from imda.models import Dataset, Source

IGNORE_KEY = "_ignore"
BASELINES_RESOURCE = "baselines.json"
_PATH_SEP = "."
Baselines = Mapping[str, Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class DriftReport:
    drifted: bool
    added_keys: tuple[str, ...] = ()
    removed_keys: tuple[str, ...] = ()
    changed: dict[str, tuple[object, object]] = field(default_factory=dict)
    note: str | None = None

    def __bool__(self) -> bool:
        return self.drifted

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"drifted": self.drifted}
        if self.added_keys:
            data["added_keys"] = list(self.added_keys)
        if self.removed_keys:
            data["removed_keys"] = list(self.removed_keys)
        if self.changed:
            data["changed"] = {
                k: {"old": old, "new": new} for k, (old, new) in self.changed.items()
            }
        if self.note:
            data["note"] = self.note
        return data

    def summary(self) -> str:
        if not self.drifted:
            return self.note or "no drift"
        parts: list[str] = []
        if self.added_keys:
            parts.append("added " + ", ".join(self.added_keys))
        if self.removed_keys:
            parts.append("removed " + ", ".join(self.removed_keys))
        parts.extend(
            f"{key}: {_short(old)} -> {_short(new)}" for key, (old, new) in self.changed.items()
        )
        return "fingerprint drift: " + "; ".join(parts)


def _short(value: object, limit: int = 80) -> str:
    text = json.dumps(value, default=str, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _normalise(value: object) -> object:
    """JSON-shaped copy: tuples become lists, so a stored baseline equals a live fingerprint."""
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(v) for v in value]
    return value


def _flatten(tree: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            flat.update(_flatten(value, f"{path}{_PATH_SEP}"))
        else:
            flat[path] = value
    return flat


def _ignored(path: str, ignore: frozenset[str]) -> bool:
    return any(path == i or path.startswith(i + _PATH_SEP) for i in ignore)


def _flat(tree: Mapping[str, object], ignore: frozenset[str]) -> dict[str, object]:
    flat = _flatten({str(k): _normalise(v) for k, v in tree.items() if k != IGNORE_KEY})
    return {k: v for k, v in flat.items() if not _ignored(k, ignore)}


def compare_fingerprint(
    baseline: Mapping[str, object] | None, current: Mapping[str, object]
) -> DriftReport:
    """Compare ``current`` to ``baseline``; never raises."""
    if not baseline:
        return DriftReport(drifted=False, note="no baseline recorded (first run)")
    listed = baseline.get(IGNORE_KEY, ())
    ignore = frozenset(str(i) for i in listed) if isinstance(listed, list | tuple) else frozenset()
    old, new = _flat(baseline, ignore), _flat(current, ignore)
    added = tuple(sorted(new.keys() - old.keys()))
    removed = tuple(sorted(old.keys() - new.keys()))
    changed = {k: (old[k], new[k]) for k in sorted(old.keys() & new.keys()) if old[k] != new[k]}
    return DriftReport(
        drifted=bool(added or removed or changed),
        added_keys=added,
        removed_keys=removed,
        changed=changed,
    )


def baseline_key(source: Source, dataset: Dataset, fingerprint: Mapping[str, object]) -> str:
    """``source/dataset``, plus ``#layout`` when the fingerprint names a page layout."""
    key = f"{source.value}/{dataset.value}"
    layout = fingerprint.get("layout")
    return f"{key}#{layout}" if isinstance(layout, str) else key


def check_drift(
    baselines: Baselines, source: Source, dataset: Dataset, fingerprint: Mapping[str, object]
) -> DriftReport:
    if fingerprint.get("row_count") == 0:
        return DriftReport(drifted=False, note="empty sample: no shape to compare")
    return compare_fingerprint(
        baselines.get(baseline_key(source, dataset, fingerprint)), fingerprint
    )


def load_baselines(path: Path | None = None) -> dict[str, dict[str, object]]:
    """Baselines keyed ``source/dataset[#layout]``; defaults to the JSON shipped in the wheel."""
    if path is not None:
        text = path.read_text(encoding="utf-8")
    else:
        text = resources.files("imda.health").joinpath(BASELINES_RESOURCE).read_text("utf-8")
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise ValueError("baselines file must hold a JSON object")
    return loaded


@lru_cache(maxsize=1)
def default_baselines() -> Baselines:
    return load_baselines()
