"""A worker asking the operator something.

A run cannot stop and wait. `claude -p` is print mode — spawned, streamed,
exited — so there is no channel back into a run once it is going, and every
agent runs `bypassPermissions`, so the CLI never pauses to ask anything either.

What an agent *can* do is finish, and leave the question behind. That is what an
issue already is: `models.IssueKind` calls a `decision` "a decision only a human
can make". This module is the one missing piece — the convention a worker uses
to raise one, since only the projectmanager has the plan protocol.

The convention is a fenced block, because a model emits one reliably and it
survives being read back as Markdown in the log:

    ```ask
    Which store backend should v2 target?
    KDS is faster but needs a second process. SQLite is already deployed.
    ```

First line is the question, the rest is detail. Anything else in the reply is
still the result — asking does not replace answering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: One run may raise a handful of questions, not a hundred. Past this the run is
#: not asking, it is looping, and the rest are dropped with a warning.
MAX_ASKS = 5
MAX_TITLE = 200
MAX_BODY = 4000

_BLOCK_RE = re.compile(r"^[ \t]*```[ \t]*ask[ \t]*\n(.*?)^[ \t]*```", re.S | re.M | re.I)

#: Appended to every task a project agent is given, so the convention travels
#: with the work rather than living in a system prompt that may not be set.
ASK_PROTOCOL = """\

---
If you need a decision only the operator can make, do not guess and do not stop \
work you can still do. Finish what you can, and put the question in a fenced \
block like this:

```ask
The one-line question
Any detail the operator needs to answer it.
```

It becomes an open issue on the project board. You will not get an answer during \
this run — the answer reaches the projectmanager on its next planning round."""


@dataclass
class Ask:
    title: str
    body: str


def parse_asks(text: str) -> tuple[list[Ask], bool]:
    """Pull every `ask` block out of a reply.

    Returns the asks and whether more were found than will be raised — a run
    that emits fifty questions is malfunctioning, and silently keeping five of
    them would hide that.
    """
    if not text:
        return [], False

    found: list[Ask] = []
    for match in _BLOCK_RE.finditer(text):
        lines = [line.rstrip() for line in match.group(1).splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue
        title = lines[0].strip().lstrip("#").strip()
        if len(title) < 3:
            continue
        body = "\n".join(lines[1:]).strip()
        found.append(Ask(title=title[:MAX_TITLE], body=body[:MAX_BODY]))

    return found[:MAX_ASKS], len(found) > MAX_ASKS


def strip_asks(text: str) -> str:
    """The reply without its ask blocks, for when only the answer is wanted.

    Not used on the stored result — the record should show what was asked — but
    kept beside the parser so the two cannot drift.
    """
    return _BLOCK_RE.sub("", text).strip()
