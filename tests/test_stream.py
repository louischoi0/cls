"""Live run output: the hub that fans it out, and the reading of `claude`'s JSON.

The rules being pinned down here are the ones that keep a browser from being
able to affect a run: publishing never blocks, a slow reader loses events rather
than stalling the worker, and nothing grows without bound.
"""

import asyncio

import pytest

from server.stream import (
    BUFFER,
    SUBSCRIBER_QUEUE,
    StreamHub,
    describe,
)


def drain(hub: StreamHub, key: str) -> list:
    """Everything a reader would see for a run that has already finished."""

    async def read():
        return [e async for e in hub.subscribe(key)]

    return asyncio.run(read())


# --- the hub ---------------------------------------------------------------- #


def test_a_run_opens_with_a_start_event_and_closes_with_an_end(hub_key="m1"):
    hub = StreamHub()
    hub.open(hub_key, "alpha")
    hub.publish(hub_key, "text", "working")
    hub.close(hub_key)

    events = drain(hub, hub_key)
    assert [e.kind for e in events] == ["start", "text", "end"]
    assert "alpha" in events[0].text
    assert [e.seq for e in events] == [1, 2, 3]


def test_a_reader_arriving_late_still_gets_the_history():
    hub = StreamHub()
    hub.open("m1", "alpha")
    for n in range(5):
        hub.publish("m1", "text", f"line {n}")
    hub.close("m1")

    assert [e.text for e in drain(hub, "m1")][1:-1] == [f"line {n}" for n in range(5)]


def test_an_unknown_run_yields_nothing_rather_than_hanging():
    assert drain(StreamHub(), "nope") == []


def test_publishing_after_close_is_ignored():
    hub = StreamHub()
    hub.open("m1", "alpha")
    hub.close("m1")
    hub.publish("m1", "text", "too late")
    assert [e.kind for e in drain(hub, "m1")] == ["start", "end"]


def test_the_history_ring_is_bounded_and_says_what_it_dropped():
    hub = StreamHub(buffer=10)
    hub.open("m1", "alpha")
    for n in range(50):
        hub.publish("m1", "text", f"line {n}")
    run = hub.get("m1")
    assert len(run.events) == 10
    assert run.dropped == 41  # 50 published + the start event, minus the 10 kept
    assert run.events[-1].text == "line 49"


def test_only_the_newest_runs_are_retained():
    hub = StreamHub(retain=3)
    for n in range(6):
        hub.open(f"m{n}", "alpha")
        hub.close(f"m{n}")
    assert drain(hub, "m0") == [] and drain(hub, "m2") == []
    assert [e.kind for e in drain(hub, "m5")] == ["start", "end"]


def test_a_live_reader_sees_events_as_they_are_published():
    async def main():
        hub = StreamHub()
        hub.open("m1", "alpha")
        seen = []

        async def read():
            async for event in hub.subscribe("m1"):
                seen.append(event.text)

        task = asyncio.create_task(read())
        await asyncio.sleep(0)  # let the subscription be registered
        hub.publish("m1", "text", "first")
        await asyncio.sleep(0)
        hub.publish("m1", "text", "second")
        hub.close("m1")
        await asyncio.wait_for(task, timeout=2)
        return seen

    assert asyncio.run(main())[1:3] == ["first", "second"]


def test_a_reader_that_never_reads_cannot_block_the_worker():
    """The whole point: a browser on a bad connection must not stall a run."""

    async def main():
        hub = StreamHub(buffer=BUFFER)
        hub.open("m1", "alpha")
        started = asyncio.Event()

        async def sluggish():
            agen = hub.subscribe("m1")
            await agen.__anext__()  # take one, then stop reading
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(sluggish())
        await started.wait()
        # Far more than one subscriber queue holds. Publishing is synchronous,
        # so if it could block, this never returns.
        for n in range(SUBSCRIBER_QUEUE * 3):
            hub.publish("m1", "text", f"line {n}")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return hub.get("m1").seq

    assert asyncio.run(asyncio.wait_for(main(), timeout=5)) == SUBSCRIBER_QUEUE * 3 + 1


def test_event_text_is_capped():
    hub = StreamHub()
    hub.open("m1", "alpha")
    hub.publish("m1", "text", "x" * 10_000)
    assert len(hub.get("m1").events[-1].text) == 2000


# --- reading the CLI's stream-json ------------------------------------------ #


def test_an_init_event_names_the_model():
    assert describe({"type": "system", "subtype": "init", "model": "claude-opus-5"}) == (
        "notice", "session started on claude-opus-5",
    )


def test_assistant_text_comes_through_as_itself():
    assert describe({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "  Hi there.  "}]},
    }) == ("text", "Hi there.")


def test_a_tool_call_names_the_tool_and_its_one_useful_field():
    kind, text = describe({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la\nmore"}},
        ]},
    })
    assert kind == "tool"
    assert text == "Bash ls -la"  # first line only


def test_a_tool_result_says_whether_it_worked_not_what_it_returned():
    """A tool result can be a whole file; the line only has room for the verdict."""
    assert describe({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "is_error": False, "content": "x" * 100_000},
        ]},
    }) == ("tool_result", "ok")
    assert describe({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "is_error": True}]},
    }) == ("tool_result", "error")


def test_the_result_event_reports_the_cost_or_the_reason_it_failed():
    assert describe({"type": "result", "total_cost_usd": 0.0769}) == ("result", "done · $0.0769")
    assert describe({
        "type": "result", "is_error": True, "subtype": "error_max_budget_usd",
        "errors": ["Reached maximum budget ($0.05)"],
    }) == ("result", "Reached maximum budget ($0.05)")


@pytest.mark.parametrize("payload", [
    {"type": "rate_limit_event"},
    {"type": "system", "subtype": "something_new"},
    {"type": "assistant", "message": {"content": []}},
    {"type": "assistant"},
    {"type": "user", "message": {"content": [{"type": "text", "text": "x"}]}},
    {"type": "a_type_that_does_not_exist_yet"},
    {},
])
def test_anything_unrecognised_is_silence_rather_than_noise(payload):
    assert describe(payload) is None
