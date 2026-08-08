"""Reading a design document as work: milestone → tasks → issues.

A document already carries the structure the project model wants. Its title is
the goal, its work lists are the tasks, and the questions it leaves open are the
issues. This module does that one reading and nothing else — it never touches
the store, so what it decides can be tested on a string.

The reading is deliberately narrow. Only a **list under a heading that names
work** becomes a task, and only a list under a heading that names an unknown
becomes an issue. Prose is left alone: a document is mostly prose, and a parser
that guessed at paragraphs would turn one design doc into fifty bad tasks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Headings whose list items are work to do.
TASK_HEADINGS = (
    "task", "todo", "to do", "to-do", "work item", "workitem", "breakdown",
    "step", "checklist", "deliverable", "action item", "acceptance criteria",
    "next up", "backlog",
)
#: Headings whose list items are things nobody has decided or resolved yet.
#:
#: Bare "decision" is deliberately absent: a "Design decisions" section lists
#: what was *settled*, and reading those back as open issues would file a
#: project's answers as its questions.
ISSUE_HEADINGS = (
    "open question", "unanswered", "risk", "blocker", "unresolved",
    "to decide", "open decision", "concern", "unknown", "tbd", "issue",
)
#: Which issue kind a heading implies. First match wins; `blocker` otherwise.
ISSUE_KINDS = (
    (("decide", "decision", "question", "unanswered", "unknown", "tbd"), "decision"),
    (("risk", "concern", "blocker", "issue", "unresolved"), "blocker"),
)

#: A document is not allowed to flood the project. Anything past these caps is
#: reported as truncated rather than silently dropped.
MAX_TASKS_PER_DOC = 60
MAX_ISSUES_PER_DOC = 40
MAX_TITLE = 200
MAX_BODY = 4000

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s*(.*)$")
#: leading "8." / "8.2" / "FR-1:" / "FR-P3:" so a heading matches on its words
_NUMBERING_RE = re.compile(r"^(?:\d+[.)]|[A-Za-z]{1,4}-[A-Za-z]?\d+:?)\s*")


@dataclass
class DocItem:
    """One list item, ready to become a task or an issue."""

    title: str
    body: str
    #: the heading it was found under, for the "where did this come from" line
    section: str


@dataclass
class DocReading:
    tasks: list[DocItem] = field(default_factory=list)
    issues: list[DocItem] = field(default_factory=list)
    #: (kind, heading) for each section that contributed
    sections: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False

    def kind_of(self, item: DocItem) -> str:
        """The issue kind implied by the heading `item` came from."""
        heading = item.section.lower()
        for words, kind in ISSUE_KINDS:
            if any(word in heading for word in words):
                return kind
        return "blocker"


def normalise_heading(text: str) -> str:
    """`## 8. Task Breakdown` -> `task breakdown`."""
    stripped = text.strip().strip("#").strip()
    stripped = _NUMBERING_RE.sub("", stripped)
    return stripped.strip(" *_`:").lower()


def classify(heading: str) -> str | None:
    """`tasks`, `issues`, or None for a heading that is neither."""
    words = normalise_heading(heading)
    if not words:
        return None
    # Issues first: "open questions about the task list" is a question section.
    if any(key in words for key in ISSUE_HEADINGS):
        return "issues"
    if any(key in words for key in TASK_HEADINGS):
        return "tasks"
    return None


def read_document(text: str) -> DocReading:
    """Split a Markdown document into the work and the unknowns it describes."""
    reading = DocReading()
    #: (level, kind) of the heading we are under; kind None means "not a
    #: section we take from", inherited by deeper headings so a sub-heading of
    #: "Task Breakdown" keeps contributing.
    stack: list[tuple[int, str | None, str]] = []
    current: list[str] | None = None  # lines of the item being accumulated
    bucket: list[DocItem] | None = None
    heading = ""
    seen_heading = False

    def flush() -> None:
        nonlocal current
        if current is None or bucket is None:
            current = None
            return
        item = _build(current, heading)
        current = None
        if item is None:
            return
        cap = MAX_TASKS_PER_DOC if bucket is reading.tasks else MAX_ISSUES_PER_DOC
        if len(bucket) >= cap:
            reading.truncated = True
            return
        bucket.append(item)

    for line in text.splitlines():
        head = _HEADING_RE.match(line)
        if head is not None:
            flush()
            level = len(head.group(1))
            title = head.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            # A leading `# ...` names the document, not a section of it. Left
            # classifiable it would swallow the whole file — "Work Instruction:
            # Projects and Tasks" reads as a task heading, and every heading
            # below it would inherit that.
            is_title = level == 1 and not seen_heading
            seen_heading = True
            kind = None if is_title else (
                classify(title) or (stack[-1][1] if stack else None)
            )
            stack.append((level, kind, title))
            heading = title
            bucket = (
                reading.tasks if kind == "tasks"
                else reading.issues if kind == "issues"
                else None
            )
            if kind and (kind, title) not in reading.sections:
                reading.sections.append((kind, title))
            continue

        if bucket is None:
            continue

        bullet = _BULLET_RE.match(line)
        if bullet is not None and not bullet.group(1):
            flush()  # a new top-level bullet ends the previous item
            current = [bullet.group(3).rstrip()]
        elif current is not None:
            if not line.strip():
                flush()  # a blank line ends the item; prose after it is context
            else:
                current.append(line.strip())

    flush()
    return reading


def _build(lines: list[str], section: str) -> DocItem | None:
    """Turn the accumulated lines of one bullet into an item, or drop it."""
    first, *rest = lines
    checkbox = _CHECKBOX_RE.match(first)
    if checkbox is not None:
        if checkbox.group(1).lower() == "x":
            return None  # already ticked: it is history, not work
        first = checkbox.group(2)
    title = _plain(first)
    if len(title) < 3:
        return None
    body = "\n".join([first, *rest]).strip()
    return DocItem(
        title=title[:MAX_TITLE], body=body[:MAX_BODY], section=section.strip()
    )


def _plain(text: str) -> str:
    """A bullet's first line as a title: no markup, no trailing colon."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text.strip().strip("*_").rstrip(":").strip()
