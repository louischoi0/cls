"""The planning round (docs/PROJECTS.md FR-P4, FR-P5).

The projectmanager plans; the server acts. The manager is invoked with the
project state and must answer with a JSON plan; every action is then validated
by `ProjectService` — the same checks the operator's own API calls go through —
and the ones that pass are applied in order.

A rejected action does not fail the round. The manager is a language model; a
half-usable plan should half-apply, with the rejections reported rather than
swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from pydantic import ValidationError

from .models import (
    MAX_PLAN_ACTIONS,
    PLAN_ACTION_ADAPTER,
    IssueCreate,
    Plan,
    PlanResult,
    ProjectAgentCreate,
    ProjectRecord,
    RejectedAction,
)
from .projects import ProjectError, ProjectService
from .runner import Job

log = logging.getLogger("cc_automation.planner")

#: Slack over the manager's own timeout_s, so the wait outlives the subprocess
#: kill and reports the timeout rather than racing it.
PLAN_WAIT_MARGIN_S = 60

#: Answered questions put in front of the manager. Enough to carry a round's
#: worth of decisions, not so many that old ones crowd out the new.
ANSWERS_SHOWN = 12

ACTION_SCHEMA = """\
{"op": "create_agent", "name": "<name>", "system_prompt": "<what it is for>",
 "allowed_tools": ["Read", ...], "cwd": "<path relative to the project root>",
 "max_budget_usd": <float>, "timeout_s": <int>}
{"op": "delete_agent", "name": "<name>"}
{"op": "create_task", "agent": "<agent name>", "title": "<short>",
 "text": "<the instruction>", "branch": "<git branch, optional>",
 "milestone_id": "<the goal this serves, optional>"}
{"op": "assign_task", "task_id": "<id from the backlog>", "agent": "<agent name>",
 "branch": "<git branch, optional>", "milestone_id": "<optional>"}
{"op": "cancel_task", "task_id": "<id>"}
{"op": "raise_issue", "title": "<what is blocking>", "body": "<detail>",
 "kind": "decision|crash|blocker", "agent": "<agent name, optional>"}
{"op": "resolve_issue", "issue_id": "<id>", "resolution": "<what settled it>"}
{"op": "note", "text": "<observation, recorded but not acted on>"}\
"""


def extract_json_object(text: str) -> dict:
    """Pull the outermost JSON object out of a model reply.

    Models wrap JSON in fences and prose. Failing a planning round over
    punctuation would be a waste of a run, so surrounding text is tolerated —
    but only a real object is accepted.
    """
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break  # not this one; try the next opening brace
                    if isinstance(parsed, dict):
                        return parsed
                    break
    raise ValueError("no JSON object found in the manager's reply")


def _first_error(exc: ValidationError) -> str:
    """One readable line out of a discriminated-union failure."""
    errors = exc.errors()
    if not errors:
        return "invalid action"
    first = errors[0]
    where = ".".join(str(p) for p in first["loc"] if not str(p).startswith("op="))
    return f"invalid action: {first['msg']}" + (f" at {where}" if where else "")


def build_prompt(
    service: ProjectService, project: ProjectRecord, note: str | None
) -> str:
    try:
        overview = service.read_overview(project)
    except ProjectError:
        overview = "(no overview.md yet)"

    agents = service.store.list_agents(project.id)
    roster = "\n".join(
        f"- {a.name} ({a.role}) cwd={a.config.cwd} tools={a.config.allowed_tools}"
        for a in agents
    ) or "(none besides you)"

    open_tasks = service.store.list_tasks(
        project.id, status="queued"
    ) + service.store.list_tasks(project.id, status="running")
    open_block = "\n".join(
        f"- {t.id} [{t.status}] {t.agent}: {t.title}" for t in open_tasks
    ) or "(none)"

    backlog = service.store.list_tasks(project.id, status="backlog", limit=60)
    backlog_block = "\n".join(
        f"- {t.id}: {t.title}" for t in backlog
    ) or "(none)"

    milestones = service.store.list_milestones(project.id)
    open_tasks_all = service.store.list_tasks(project.id, limit=1000)
    milestone_block = "\n".join(
        f"- {m.id} [{m.status}] {m.title}"
        + (f" — target: {m.target}" if m.target else "")
        + (f"\n    {m.body.strip()[:300]}" if m.body.strip() else "")
        + "\n    tasks: {} done / {} total".format(
            sum(1 for t in open_tasks_all
                if t.milestone_id == m.id and t.status == "done"),
            sum(1 for t in open_tasks_all if t.milestone_id == m.id),
        )
        for m in milestones
    ) or "(none — the operator has not set any goals yet)"

    issues = service.store.list_issues(project.id, status="open")
    issues_block = "\n".join(
        f"- {i.id} [{i.kind}] {i.title}"
        + (f" (agent {i.agent})" if i.agent else "")
        + (f"\n    {i.body.strip()[:300]}" if i.body.strip() else "")
        for i in issues
    ) or "(none)"

    # The operator's answers. Without these the console's promise — "the manager
    # reads this on its next planning round" — would simply not be true: a
    # resolved issue leaves the open list and is never seen again.
    answered = [
        i for i in service.store.list_issues(project.id, status="resolved", limit=200)
        if i.resolution
    ][:ANSWERS_SHOWN]
    answers_block = "\n".join(
        f"- {i.title}\n    answer: {i.resolution.strip()[:400]}" for i in answered
    ) or "(none)"

    finished = [
        t
        for t in service.store.list_tasks(project.id, limit=40)
        if t.status in ("done", "failed", "cancelled")
    ][:10]
    finished_block = "\n".join(
        f"- {t.id} [{t.status}] {t.agent}: {t.title}\n"
        f"    {((t.result or t.error or '').strip().replace(chr(10), ' '))[:400]}"
        for t in finished
    ) or "(none)"

    return f"""\
You are the projectmanager for project "{project.name}" (id: {project.id}).
Your job is to keep the project moving toward what overview.md describes, by
maintaining a set of worker agents and assigning them tasks.

# overview.md
{overview}

# Current agents
{roster}

# Milestones — the operator's goals. You cannot create, edit or close these;
# your job is to move them forward by creating tasks against them.
{milestone_block}

# Open tasks
{open_block}

# Backlog — work already written down, given to nobody. Most of this came from
# the project's own documents. Assign it rather than restating it as a new task.
{backlog_block}

# Open issues — these are what is blocking the project. A `decision` raised by
# an agent is a question waiting on the operator; you cannot answer it yourself.
{issues_block}

# Answered — the operator has since decided these. Act on them: this is the only
# time they will be put in front of you.
{answers_block}

# Recently finished tasks
{finished_block}

# Rules
- Worker agents may only be given tools from this project's policy:
  {project.tool_policy}
- `cwd` must stay inside the project root ({project.root_dir}).
- You cannot create, delete, or assign work to a projectmanager.
- At most {MAX_PLAN_ACTIONS} actions per plan.
- Do not re-create an agent that already exists, and do not duplicate a task
  that is already open. If the backlog already describes the work, `assign_task`
  it; do not write it out again as a new task.
- Prefer few, well-scoped tasks. A task's `text` is the entire instruction the
  worker will receive; it has no other context.
- A **milestone** is a goal the operator set. You cannot create or close one —
  put `milestone_id` on the tasks you create so the work counts toward it, and
  raise an issue if a goal is blocked or looks wrong.
- An **issue** is something blocking progress that a task cannot simply do: a
  decision someone has to make, or a run that broke unexpectedly. Raise one
  instead of inventing a task when you are blocked or need a human. Resolve one
  when the plan you are making settles it.
{f"- The operator adds: {note}" if note else ""}

# Reply format
Reply with ONE JSON object and nothing else:

{{"summary": "<why this plan>", "actions": [ ... ]}}

Each action is one of:
{ACTION_SCHEMA}

If nothing needs doing, reply with an empty actions list and say why in the
summary.
"""


async def run_plan(
    service: ProjectService, project: ProjectRecord, note: str | None
) -> PlanResult:
    agents = service.store.list_agents(project.id)
    manager = next((a for a in agents if a.role == "manager"), None)
    if manager is None:
        raise ProjectError(409, f"project {project.id!r} has no projectmanager")

    prompt = build_prompt(service, project, note)
    message_id = uuid.uuid4().hex[:16]
    topic = f"{project.id}-plan"
    service.status.create(message_id, topic, [f"project:{project.id}"], [manager.runtime_name])

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    # Through the manager's own queue, not around it: two overlapping plan
    # requests must not resume the same session concurrently (README FR-3).
    await service.dispatcher.enqueue(
        manager.runtime_name,
        Job(
            message_id=message_id,
            agent=manager.runtime_name,
            text=prompt,
            topic=topic,
            future=future,
        ),
    )

    timeout = manager.config.timeout_s + PLAN_WAIT_MARGIN_S
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ProjectError(
            504, f"the projectmanager did not answer within {timeout}s"
        ) from exc

    if not result.ok:
        raise ProjectError(502, f"the projectmanager run failed: {result.result_text}")

    return await apply_reply(service, project, message_id, result.result_text)


async def apply_reply(
    service: ProjectService, project: ProjectRecord, message_id: str, reply: str
) -> PlanResult:
    outcome = PlanResult(project_id=project.id, message_id=message_id, raw_reply=reply)
    try:
        plan = Plan.model_validate(extract_json_object(reply))
    except Exception as exc:
        raise ProjectError(422, f"could not read the manager's plan: {exc}") from exc

    outcome.summary = plan.summary
    actions = plan.actions
    if len(actions) > MAX_PLAN_ACTIONS:
        for raw in actions[MAX_PLAN_ACTIONS:]:
            outcome.rejected.append(
                RejectedAction(
                    action=raw,
                    reason=f"over the {MAX_PLAN_ACTIONS}-action limit for one plan",
                )
            )
        actions = actions[:MAX_PLAN_ACTIONS]

    for raw in actions:
        try:
            action = PLAN_ACTION_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            outcome.rejected.append(
                RejectedAction(action=raw, reason=_first_error(exc))
            )
            continue
        try:
            await _apply_one(service, project, action, outcome)
        except ProjectError as exc:
            outcome.rejected.append(RejectedAction(action=raw, reason=exc.message))
            continue
        except Exception as exc:  # a bad action must not sink the whole plan
            log.exception("plan action failed for project %s", project.id)
            outcome.rejected.append(RejectedAction(action=raw, reason=str(exc)))
            continue
        outcome.applied.append(raw)

    log.info(
        "project %s: plan applied %d, rejected %d",
        project.id, len(outcome.applied), len(outcome.rejected),
        extra={"message_id": message_id},
    )
    return outcome


async def _apply_one(
    service: ProjectService, project: ProjectRecord, action, outcome: PlanResult
) -> None:
    op = action.op

    if op == "note":
        return

    if op == "create_agent":
        spec = ProjectAgentCreate(
            name=action.name,
            role="worker",  # a project has exactly one manager, and it is not
                            # the manager's to appoint
            system_prompt=action.system_prompt,
            allowed_tools=action.allowed_tools,
            cwd=action.cwd,
            **{
                k: v
                for k, v in (
                    ("max_budget_usd", action.max_budget_usd),
                    ("timeout_s", action.timeout_s),
                )
                if v is not None
            },
        )
        service.add_agent(project, spec)
        return

    if op == "delete_agent":
        record = service.require_agent(project, action.name)
        if record.role == "manager":
            raise ProjectError(422, "the projectmanager cannot delete itself")
        await service.delete_agent(project, action.name)
        return

    if op == "create_task":
        task = await service.create_task(
            project, action.agent, action.title, action.text,
            created_by="manager", branch=action.branch,
            milestone_id=action.milestone_id,
        )
        outcome.tasks_created.append(task.id)
        return

    if op == "raise_issue":
        issue = service.raise_issue(
            project,
            IssueCreate(title=action.title, body=action.body, kind=action.kind,
                        agent=action.agent, branch=action.branch),
            created_by="manager",
        )
        outcome.issues_raised.append(issue.id)
        return

    if op == "resolve_issue":
        issue = service.store.get_issue(action.issue_id)
        if issue is None or issue.project_id != project.id:
            raise ProjectError(404, f"unknown issue {action.issue_id!r} in this project")
        service.close_issue(action.issue_id, action.resolution, dismiss=False)
        return

    if op == "assign_task":
        if service.require_agent(project, action.agent).role == "manager":
            raise ProjectError(422, "the projectmanager may not assign tasks to itself")
        task = await service.assign_task(
            project, action.task_id, action.agent,
            branch=action.branch, milestone_id=action.milestone_id,
        )
        outcome.tasks_created.append(task.id)
        return

    if op == "cancel_task":
        task = service.store.get_task(action.task_id)
        if task is None or task.project_id != project.id:
            raise ProjectError(404, f"unknown task {action.task_id!r} in this project")
        service.cancel_task(action.task_id)
        return

    raise ProjectError(422, f"unsupported action {op!r}")
