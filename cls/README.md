# cls/

Written by the cls server. **Do not edit** — every file here is regenerated
whenever the agent it describes changes state, and edits are overwritten.

- `agents/<name>.json` — one agent: its configuration, what it is doing right
  now, what is queued behind it, and what it has cost so far.

The server never reads this directory back; the database is the record.
