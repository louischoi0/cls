"""Markdown work logs at logs/{YYYY-MM-DD}/{topic}.md.

The runner writes these, not the agent (README FR-4): the entry is derived from
the JSON result of each run, so a log line exists whether or not Claude decided
to cooperate.

`slugify` and `validate_date` are used on both the write and the read path, so
the two cannot drift apart and open a hole on one side only.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_UNSAFE_RUN = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_LEN = 80


class LogStoreError(ValueError):
    """Rejected topic or date. Surfaced as 400."""


def slugify(topic: str) -> str:
    """Normalise a topic to `[a-z0-9-]`, rejecting anything path-shaped."""
    if not topic or not topic.strip():
        raise LogStoreError("topic must not be blank")
    if any(sep in topic for sep in ("/", "\\", "\x00")) or ".." in topic:
        raise LogStoreError(f"topic contains a path component: {topic!r}")

    slug = _UNSAFE_RUN.sub("-", topic.strip().lower()).strip("-")[:_MAX_SLUG_LEN]
    slug = slug.strip("-")
    if not slug:
        raise LogStoreError(f"topic has no usable characters: {topic!r}")
    return slug


def validate_date(value: str) -> str:
    # strptime alone accepts "2026-8-7"; the round-trip check pins the format to
    # exactly the one the log directories use.
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise LogStoreError(f"date must be YYYY-MM-DD, got {value!r}") from exc
    canonical = parsed.strftime("%Y-%m-%d")
    if canonical != value:
        raise LogStoreError(f"date must be YYYY-MM-DD, got {value!r}")
    return canonical


class LogStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[Path, asyncio.Lock] = defaultdict(asyncio.Lock)

    def path_for(self, date: str, topic: str) -> Path:
        """Resolve a log path, with containment as a second barrier."""
        path = (self.root / validate_date(date) / f"{slugify(topic)}.md").resolve()
        if not path.is_relative_to(self.root):
            raise LogStoreError("resolved path escapes the log root")
        return path

    async def append_entry(
        self,
        *,
        date: str,
        topic: str,
        when: datetime,
        agent: str,
        message_id: str,
        text: str,
        result: str,
        duration_s: float,
        cost_usd: float | None,
        status: str,
    ) -> Path:
        path = self.path_for(date, topic)
        cost = "unknown" if cost_usd is None else f"{cost_usd:.4f}"
        entry = (
            f"## [{when.strftime('%H:%M:%S')}] agent: {agent} | message: {message_id}\n\n"
            f"**Input:** {text}\n\n"
            f"**Result:**\n{result}\n\n"
            f"**Meta:** duration={duration_s:.1f}s, cost_usd={cost}, status={status}\n\n"
            f"---\n\n"
        )

        async with self._locks[path]:
            path.parent.mkdir(parents=True, exist_ok=True)
            # One open + one write per entry, so a concurrent append can never
            # interleave inside an entry.
            with path.open("a", encoding="utf-8") as fh:
                fh.write(entry)
        return path

    def list_dates(self) -> list[str]:
        dates = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            try:
                dates.append(validate_date(child.name))
            except LogStoreError:
                continue
        return sorted(dates)

    def list_topics(self, date: str) -> list[str]:
        day = (self.root / validate_date(date)).resolve()
        if not day.is_relative_to(self.root) or not day.is_dir():
            return []
        return sorted(p.stem for p in day.glob("*.md"))

    def read_topic(self, date: str, topic: str) -> str | None:
        path = self.path_for(date, topic)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
