"""The work profile: what the bot learns from how the Mayor actually works.

Every finished task is a data point: when it was really done, how many times
it was pushed back, and how long it took against the estimate. Tasks are
grouped by their `kind` (e.g. "woocommerce", "content", "packing"), falling
back to the building, so the bot can nudge each kind of work at the hour the
Mayor usually does it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from statistics import median
from zoneinfo import ZoneInfo

from .buildings import label
from .db import Task, parse_iso

MIN_SAMPLES = 3


@dataclass
class Pattern:
    group: str
    samples: int
    usual_time: time | None
    avg_postpones: float
    estimate_ratio: float | None  # actual / estimate, e.g. 1.6 = takes 60% longer


def group_of(task: Task) -> str:
    return task.kind or task.building


def build_profile(done: list[Task], tz: ZoneInfo) -> dict[str, Pattern]:
    groups: dict[str, list[Task]] = {}
    for t in done:
        if t.done_at:
            groups.setdefault(group_of(t), []).append(t)

    profile = {}
    for group, tasks in groups.items():
        minutes = []
        for t in tasks:
            local = parse_iso(t.done_at).astimezone(tz)
            minutes.append(local.hour * 60 + local.minute)
        mid = int(median(minutes))
        ratios = [t.actual_min / t.estimate_min for t in tasks if t.actual_min and t.estimate_min]
        profile[group] = Pattern(
            group=group,
            samples=len(tasks),
            usual_time=time(mid // 60, mid % 60) if len(tasks) >= MIN_SAMPLES else None,
            avg_postpones=sum(t.postpone_count for t in tasks) / len(tasks),
            estimate_ratio=sum(ratios) / len(ratios) if ratios else None,
        )
    return profile


def suggest_time(task: Task, profile: dict[str, Pattern]) -> time | None:
    """The hour this kind of task usually gets done, once there is enough data."""
    for key in (task.kind, task.building):
        p = profile.get(key) if key else None
        if p and p.usual_time:
            return p.usual_time
    return None


def describe(profile: dict[str, Pattern]) -> str:
    """Human-readable profile, also fed to Claude as context."""
    if not profile:
        return "هنوز داده کافی ندارم. هر کاری که با ✅ تموم کنی، یه چیز درباره روال کارت یادم می‌ده."
    lines = []
    for p in sorted(profile.values(), key=lambda p: -p.samples):
        name = label(p.group)
        parts = [f"{p.samples} بار انجام شده"]
        if p.usual_time:
            parts.append(f"معمولاً حدود {p.usual_time:%H:%M}")
        if p.avg_postpones >= 1:
            parts.append(f"به‌طور میانگین {p.avg_postpones:.1f} بار عقب افتاده")
        if p.estimate_ratio and abs(p.estimate_ratio - 1) >= 0.2:
            parts.append(f"زمان واقعی {p.estimate_ratio:.1f} برابر تخمینه")
        lines.append(f"• {name}: " + "، ".join(parts))
    return "\n".join(lines)


def often_postponed(profile: dict[str, Pattern], threshold: float = 2.0) -> list[str]:
    return [p.group for p in profile.values() if p.avg_postpones >= threshold and p.samples >= 2]
