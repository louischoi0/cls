# Work Instruction: KDS as the project store

**Date:** 2026-08-07
**Status:** Draft v1 (extends `docs/PROJECTS.md` §5)
**Owner:** Louis

---

## 1. Purpose

Run the project store on **KDS** (github.com/louischoi0/ckdbs) instead of
SQLite. KDS is an OLTP engine with a deliberately narrow surface; this document
records what that surface costs the store, what is done about each cost, and the
one guarantee that is genuinely weaker as a result.

`docs/PROJECTS.md` §5 specified SQLite. This supersedes the storage half of it;
the schema's *meaning* is unchanged.

## 2. Selecting a backend

`ProjectService` talks only to the `ProjectStore` interface, so the backend is a
URL in `CC_AUTOMATION_STORE`:

```
kds://127.0.0.1:15432?fallback=sqlite      # what the server runs on by default
sqlite://state/projects.db                 # what the tests run on
```

`Config.from_env()` — the path uvicorn takes — defaults to the KDS URL. A
`Config(...)` constructed in code defaults to SQLite, so the test suite never
depends on an engine being up.

**`fallback=sqlite` is opt-in and loud.** Without it, an unreachable
`kds_server` is a startup failure naming the address. With it, the server logs
at `ERROR` and comes up on SQLite. The two databases do not sync, so anything
written while the fallback is live is not in KDS; `GET /health` reports
`{"store": "kds"|"sqlite"}` so which one is live is never a guess.

## 3. What KDS does not have, and what is done instead

| Missing | Why it matters here | What this store does |
|---|---|---|
| `UNIQUE`, `CREATE INDEX` | Three uniqueness rules were schema constraints | Checks inside a transaction under the connection lock — see §4 |
| `NULL` | Nine columns are nullable | The sentinel cell `~` |
| String escaping | A task instruction contains apostrophes | Base64 — see §5 |
| Values over 8144 bytes | A task result routinely exceeds it | Chunked into `blobs` — see §6 |
| `ORDER BY`, `LIMIT` | `list_tasks` needs both | Sorted and sliced in Python |
| `ON DELETE CASCADE` | `delete_project` relied on it | Children deleted explicitly, in order |
| `DROP TABLE` | Test isolation | An unqualified `DELETE FROM`, and one server per session |
| Secondary indexes | Every lookup is a scan | Cabins on the filtered columns — see §7 |

## 4. The guarantee that is weaker

SQLite enforces three rules in the schema, where they cannot be forgotten:

```sql
UNIQUE (project_id, name)
UNIQUE (runtime_name)
CREATE UNIQUE INDEX one_manager_per_project ON agents(project_id) WHERE role = 'manager';
```

KDS has no `UNIQUE` and, by design, no `CREATE INDEX`. The KDS backend therefore
reads before it writes, inside a `BEGIN`/`COMMIT`, holding the same lock that
serialises its one connection.

**This is correct for this server and strictly weaker in general.** One process
holding one connection cannot interleave with itself, so the check and the
insert are atomic here. A second writer against the same database could slip
between them and create a project's second projectmanager. Nothing detects that
afterwards.

Accepting this is a deliberate trade, not an oversight: the system is
single-operator and single-process (README §2), and the alternative is not
running on KDS at all. If a second writer ever exists, this is the thing that
breaks first.

`tests/test_store_contract.py` runs the same suite against both backends, so
the *behaviour* is verified identical even though the enforcement is not.

## 5. Encoding: nothing reaches the wire as text

Three properties of the KWP text protocol force this, all verified against the
running server rather than read off the docs:

1. A string literal is `'single quoted'` with **no escape mechanism** — `''` and
   `\'` are both parse errors. A value containing an apostrophe cannot be
   written at all.
2. A `SELECT` reply is bare CSV with no quoting. A comma in a value silently
   splits it into two cells; the row separator is the literal two-character
   `\n`. `tools/ckdbs_cli.py` documents this about its own parser.
3. `NULL` is refused: *"NULL values are not supported yet"*.

So every value is base64 before it is sent. The base64 alphabet
(`A-Za-z0-9+/=`) contains no quote, comma, backslash or newline, which is what
makes the naive split in `kwp.py` exactly correct rather than usually correct —
and `kwp.select()` refuses a row whose cell count is wrong instead of
mis-attributing it.

Two markers ride alongside, both starting with `~`, which base64 never produces:

| Cell | Meaning |
|---|---|
| `~` | The value is `None` |
| `~b<hex>` | The value is chunked into `blobs` |
| anything else | base64, possibly of the empty string |

`""` and `None` therefore stay distinct, which the contract suite checks.

## 6. Large values

One var-heap page is 8144 bytes and a longer value is refused outright, so a
cell is capped at 7900 encoded bytes — roughly 5.9KB of text. Task results
exceed that regularly.

Anything longer is base64'd **first** and the *encoded* string is split, so a
chunk boundary can never land inside a multi-byte character. Chunks go to
`blobs(blob_id, seq, chunk)` and the column holds `~b<blob_id>`. Reads reassemble
in `seq` order. Overwriting a spilled cell deletes the old chunks first;
deleting a row or a project releases them.

Verified to 60KB in both directions.

## 7. Schema

Column 0 of every relation is the engine's Keystone id: an integer it assigns,
never supplied by `INSERT`. This store addresses rows by its own string ids and
only reads `rid` back.

```
projects (rid int64, id, name, root_dir, tool_policy, created_at)
agents   (rid int64, project_id, name, runtime_name, role, config, created_at)
tasks    (rid int64, id, project_id, agent, title, body, status, created_by,
          message_id, result, error, cost_usd, created_at, started_at, finished_at)
blobs    (rid int64, blob_id, seq int64, chunk)
```

All `varchar`, all `BTREE`. `body` rather than `text`, so no column name meets
the grammar. `cost_usd` is a string because `float` is refused — under the
fixed-length rule its on-disk width is still an open decision.

`CREATE TABLE` is idempotent (`EXISTS oid=<n>`), so schema init runs on every
boot.

Because there are no secondary indexes, a **Cabin** is declared on each column
this store filters by: `projects(id)`, `agents(project_id)`, `agents(runtime_name)`,
`tasks(id)`, `tasks(project_id)`, `tasks(status)`, `tasks(agent)`, `blobs(blob_id)`.
A Cabin is authoritative for the values it has observed, which is the engine's
own answer to the indexes it does not have.

## 8. Durability

`INSERT` is the only logged statement. `CREATE TABLE`, `UPDATE` and `DELETE`
survive the process dying only after a `SYNC`, so the store issues one after
schema init and one on close. A crash between them can lose status transitions
that inserts of the same age would keep.

This is weaker than the SQLite backend, where every write is in the WAL. It is
worth knowing before treating a `running` task row as authoritative after a hard
kill — though the startup sweep marks those `failed` anyway.

## 9. Acceptance criteria

- [x] The same contract suite passes against both backends.
- [x] A value with apostrophes, commas and newlines round-trips intact.
- [x] A 60KB task result round-trips intact.
- [x] `None` and `""` stay distinct.
- [x] All three uniqueness rules raise `StoreError` with the same messages.
- [x] `cancel_if_queued` is a compare-and-set, not a read-then-write.
- [x] Projects and agents survive a server restart against a live engine.
- [x] An unreachable engine either fails startup by name or falls back loudly.
- [x] `GET /health` reports which backend is live.

## 10. Out of scope

- Multi-process safety for the uniqueness rules (§4).
- KDS foreign keys: they reference the parent's Keystone id, but this store
  relates rows by its own string ids, so they cannot express these relationships.
- The KWP/1 binary protocol, once it lands; this uses the newline text protocol.
- Migrating data between the two backends.
