"""Reading a Markdown document as work (server/docimport.py).

Pure text in, items out — no store, no HTTP. What this file pins down is the
line between "a design document" and "fifty bad tasks".
"""

import pytest

from server.docimport import classify, normalise_heading, read_document


def titles(items) -> list[str]:
    return [i.title for i in items]


# --- which headings count ---------------------------------------------------- #


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("## 8. Task Breakdown", "tasks"),
        ("## Acceptance Criteria", "tasks"),
        ("### To do", "tasks"),
        ("## Checklist", "tasks"),
        ("## Open questions", "issues"),
        ("## Risks", "issues"),
        ("## Known issues", "issues"),
        ("## 2. Purpose", None),
        ("## Architecture", None),
        # A "Design decisions" section lists what was settled. Reading those
        # back as open issues would file a project's answers as its questions.
        ("## Design decisions", None),
    ],
)
def test_headings_are_classified_by_what_they_name(heading, expected):
    assert classify(heading) == expected


def test_numbering_and_requirement_ids_are_stripped_before_matching():
    assert normalise_heading("## 8. Task Breakdown") == "task breakdown"
    assert normalise_heading("### FR-P3: Tasks") == "tasks"


# --- what comes out of a document -------------------------------------------- #

DOC = """\
# Work Instruction: Projects and Tasks

Prose. Not a task.

## 3. Task Breakdown

1. **Scaffold** — the app shell.
2. Wire the API client.
   It reads the key from the header.

   Prose after a blank line is context, not a second task.
3. no

## Open questions

- Who owns deploys?
"""


def test_a_document_yields_its_work_and_its_unknowns():
    reading = read_document(DOC)
    assert titles(reading.tasks) == ["Scaffold — the app shell.", "Wire the API client."]
    assert titles(reading.issues) == ["Who owns deploys?"]
    assert reading.sections == [
        ("tasks", "3. Task Breakdown"),
        ("issues", "Open questions"),
    ]


def test_the_title_heading_never_classifies_the_whole_document():
    """`# Work Instruction: ... and Tasks` reads as a task heading.

    Left classifiable it would swallow the file, because every heading below it
    inherits the kind of the one above.
    """
    reading = read_document(DOC)
    assert all(i.section != "Work Instruction: Projects and Tasks" for i in reading.tasks)


def test_a_bullet_carries_its_indented_lines_but_stops_at_a_blank():
    item = next(t for t in read_document(DOC).tasks if t.title == "Wire the API client.")
    assert "It reads the key from the header." in item.body
    assert "Prose after a blank line" not in item.body


def test_a_ticked_checkbox_is_history_and_an_unticked_one_is_work():
    reading = read_document(
        "## Acceptance criteria\n\n- [x] Already done.\n- [ ] Still to do.\n"
    )
    assert titles(reading.tasks) == ["Still to do."]


def test_a_sub_heading_inherits_the_section_it_sits_under():
    reading = read_document(
        "# Doc\n\n## Task breakdown\n\n### Phase one\n\n- Build the thing.\n"
    )
    assert titles(reading.tasks) == ["Build the thing."]
    assert reading.tasks[0].section == "Phase one"


def test_prose_outside_a_matching_section_is_left_alone():
    reading = read_document(
        "# Doc\n\n## Background\n\n- A bullet that is not work.\n\n## Design\n\n- Nor this.\n"
    )
    assert reading.tasks == [] and reading.issues == []


def test_a_title_is_stripped_of_its_markup():
    reading = read_document("## Tasks\n\n- **Ship** the `console` to [prod](http://x)\n")
    assert titles(reading.tasks) == ["Ship the console to prod"]


def test_the_kind_of_an_issue_comes_from_its_heading():
    reading = read_document(
        "# D\n\n## Open questions\n\n- Which store?\n\n## Risks\n\n- KDS may be down.\n"
    )
    kinds = {i.title: reading.kind_of(i) for i in reading.issues}
    assert kinds == {"Which store?": "decision", "KDS may be down.": "blocker"}


def test_a_flood_of_items_is_capped_and_reported():
    from server.docimport import MAX_TASKS_PER_DOC

    body = "\n".join(f"- Item number {n}" for n in range(MAX_TASKS_PER_DOC + 10))
    reading = read_document(f"# D\n\n## Tasks\n\n{body}\n")
    assert len(reading.tasks) == MAX_TASKS_PER_DOC
    assert reading.truncated is True


def test_an_empty_document_reads_as_nothing():
    reading = read_document("")
    assert reading.tasks == [] and reading.issues == [] and not reading.truncated
