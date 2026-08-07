import asyncio
from datetime import datetime

import pytest

from server.logstore import LogStore, LogStoreError, slugify, validate_date


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Daily Standup", "daily-standup"),
        ("  Mixed_Case 42 ", "mixed-case-42"),
        ("a---b", "a-b"),
        ("!!!topic!!!", "topic"),
    ],
)
def test_slugify_normalises(raw, expected):
    assert slugify(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["../../etc/passwd", "/etc/passwd", "a/b", "a\\b", "..", "", "   ", "!!!"],
)
def test_slugify_rejects_path_shaped_and_empty(raw):
    with pytest.raises(LogStoreError):
        slugify(raw)


def test_slugify_is_length_capped():
    assert len(slugify("x" * 500)) <= 80


@pytest.mark.parametrize("raw", ["2026-08-07", "2026-01-01"])
def test_validate_date_accepts(raw):
    assert validate_date(raw) == raw


@pytest.mark.parametrize("raw", ["2026-8-7", "not-a-date", "../2026-08-07", "2026-13-01"])
def test_validate_date_rejects(raw):
    with pytest.raises(LogStoreError):
        validate_date(raw)


def test_path_stays_inside_root(tmp_path):
    store = LogStore(tmp_path / "logs")
    path = store.path_for("2026-08-07", "topic")
    assert path.is_relative_to(store.root)
    with pytest.raises(LogStoreError):
        store.path_for("2026-08-07", "../../escape")


def _append(store, **kw):
    defaults = dict(
        date="2026-08-07",
        topic="smoke",
        when=datetime(2026, 8, 7, 12, 30, 45),
        agent="alpha",
        message_id="m1",
        text="hello",
        result="world",
        duration_s=1.5,
        cost_usd=0.0123,
        status="ok",
    )
    defaults.update(kw)
    return store.append_entry(**defaults)


def test_entry_matches_the_documented_format(tmp_path):
    store = LogStore(tmp_path / "logs")
    path = asyncio.run(_append(store))
    content = path.read_text()
    assert content.startswith("## [12:30:45] agent: alpha | message: m1\n")
    assert "**Input:** hello" in content
    assert "**Result:**\nworld" in content
    assert "**Meta:** duration=1.5s, cost_usd=0.0123, status=ok" in content
    assert content.rstrip().endswith("---")


def test_unknown_cost_is_labelled(tmp_path):
    store = LogStore(tmp_path / "logs")
    path = asyncio.run(_append(store, cost_usd=None, status="failed"))
    assert "cost_usd=unknown, status=failed" in path.read_text()


def test_concurrent_appends_do_not_interleave(tmp_path):
    store = LogStore(tmp_path / "logs")

    async def main():
        await asyncio.gather(
            *(_append(store, message_id=f"m{i}", result=f"r{i}") for i in range(25))
        )

    asyncio.run(main())
    content = store.read_topic("2026-08-07", "smoke")
    assert content.count("**Meta:**") == 25
    # Every header is immediately followed by its own Input line.
    for block in content.split("## ")[1:]:
        assert block.count("**Input:**") == 1
        assert block.count("**Meta:**") == 1


def test_queries_list_and_read(tmp_path):
    store = LogStore(tmp_path / "logs")
    asyncio.run(_append(store, topic="alpha-work"))
    asyncio.run(_append(store, date="2026-08-08", topic="beta-work"))

    assert store.list_dates() == ["2026-08-07", "2026-08-08"]
    assert store.list_topics("2026-08-07") == ["alpha-work"]
    assert store.list_topics("2026-08-09") == []
    assert store.read_topic("2026-08-07", "alpha-work").startswith("## [")
    assert store.read_topic("2026-08-07", "missing") is None


def test_list_dates_ignores_non_date_directories(tmp_path):
    store = LogStore(tmp_path / "logs")
    (store.root / "not-a-date").mkdir()
    assert store.list_dates() == []
