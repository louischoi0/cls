"""A worker asking the operator something (server/asks.py).

Pure text in, questions out. The rule this pins down is that asking is a
deliberate, fenced act — prose that happens to contain a question mark is not a
question the operator has to answer.
"""

import pytest

from server.asks import ASK_PROTOCOL, MAX_ASKS, parse_asks, strip_asks

ONE = """\
I read the schema and wrote the migration.

```ask
Which store backend should v2 target?
KDS is faster but needs a second process. SQLite is already deployed.
```

The migration is on feat/store.
"""


def test_a_fenced_block_becomes_a_question():
    asks, too_many = parse_asks(ONE)
    assert too_many is False
    assert len(asks) == 1
    assert asks[0].title == "Which store backend should v2 target?"
    assert asks[0].body.startswith("KDS is faster")


def test_a_question_without_detail_is_still_a_question():
    asks, _ = parse_asks("```ask\nShip it or wait?\n```")
    assert asks[0].title == "Ship it or wait?" and asks[0].body == ""


def test_several_questions_come_out_in_order():
    text = "```ask\nFirst?\n```\nprose\n```ask\nSecond?\n```"
    asks, _ = parse_asks(text)
    assert [a.title for a in asks] == ["First?", "Second?"]


@pytest.mark.parametrize("text", [
    "",
    "Should I use SQLite or KDS? I went with SQLite.",   # prose is not an ask
    "```\nShip it?\n```",                                 # an unlabelled fence
    "```python\nask('Ship it?')\n```",                    # a code sample
    "```ask\n\n```",                                      # empty
    "```ask\nno\n```",                                    # too short to be one
])
def test_only_a_deliberate_block_counts(text):
    assert parse_asks(text) == ([], False)


def test_a_run_that_floods_is_capped_and_says_so():
    """Fifty questions is a malfunction, and keeping five silently would hide it."""
    text = "\n".join(f"```ask\nQuestion number {n}?\n```" for n in range(MAX_ASKS + 4))
    asks, too_many = parse_asks(text)
    assert len(asks) == MAX_ASKS and too_many is True


def test_an_indented_block_is_still_found():
    asks, _ = parse_asks("- a list item\n  ```ask\n  Nested?\n  ```\n")
    assert [a.title for a in asks] == ["Nested?"]


def test_the_reply_can_be_read_without_its_questions():
    assert strip_asks(ONE).startswith("I read the schema")
    assert "Which store backend" not in strip_asks(ONE)


def test_the_protocol_shows_the_exact_shape_it_parses():
    """The instruction agents are given must be an example this parser accepts,
    or every worker is taught a convention that does not work."""
    asks, _ = parse_asks(ASK_PROTOCOL)
    assert [a.title for a in asks] == ["The one-line question"]
