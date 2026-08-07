"""Getting arbitrary Python values through a protocol with no escaping.

Three facts about the KDS wire decide everything here:

1. A string literal is `'single quoted'` and there is **no escape** — `''` and
   `\\'` are both parse errors, so a value containing an apostrophe cannot be
   written at all.
2. A SELECT reply is bare CSV; a comma in a value silently splits it into two
   cells, and the literal two-character `\\n` is the row separator.
3. `NULL` is refused: "NULL values are not supported yet".

So no value is ever sent as text. Everything is base64 — whose alphabet
(`A-Za-z0-9+/=`) contains no quote, comma, backslash or newline — which makes
the naive split in `kwp.py` exactly correct. Two markers ride alongside it,
both starting with `~`, a character base64 never produces:

    ~            the value is None
    ~b<hex>      the value is too big for one cell and lives in `blobs`

A cell that is neither is base64, possibly of the empty string.
"""

from __future__ import annotations

import base64

NULL_CELL = "~"
BLOB_PREFIX = "~b"

#: One var-heap page is 8144 bytes and a longer value is refused outright, so
#: anything above this is chunked. Left of the limit deliberately: the cap is
#: on the encoded byte count, and there is no reason to sail close to it.
MAX_CELL = 7900


def encode(value: str) -> str:
    """Text -> a cell body safe for both the literal and the reply grammar."""
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def decode(cell: str) -> str:
    return base64.b64decode(cell.encode("ascii")).decode("utf-8")


def chunks(encoded: str) -> list[str]:
    """Split an over-long encoded value.

    Slicing the base64 rather than the source text is what keeps this safe:
    base64 is ASCII, so a chunk boundary can never land inside a multi-byte
    character.
    """
    return [encoded[i : i + MAX_CELL] for i in range(0, len(encoded), MAX_CELL)]


def is_blob(cell: str) -> bool:
    return cell.startswith(BLOB_PREFIX)


def blob_id(cell: str) -> str:
    return cell[len(BLOB_PREFIX) :]


def quote(cell: str) -> str:
    """Wrap a cell body as a SQL string literal.

    Everything reaching here has been through `encode` or is a marker, so there
    is nothing to escape — but a stray quote would end the literal early and
    turn the rest of a value into syntax, so it is refused rather than trusted.
    """
    if "'" in cell or "\n" in cell or "\\" in cell:
        raise ValueError(f"unencoded value reached the wire: {cell[:60]!r}")
    return f"'{cell}'"
