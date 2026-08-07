"""Client for the KDS wire protocol (github.com/louischoi0/ckdbs).

One command per line in, one line back. Replies to `SELECT` are CSV with a
header row, rows joined by the literal two-character escape `\\n` rather than a
newline byte.

The protocol has **no quoting or escaping**, in either direction: a string
literal is single-quoted with no way to write a quote inside it, and a reply
cell containing a comma would be mis-split (`tools/ckdbs_cli.py` says as much
about its own parser). Values are therefore never sent as-is — everything goes
through `codec.py`, which encodes into an alphabet with no comma, quote,
backslash or newline in it. That is what makes the naive split below correct
rather than merely usually correct.
"""

from __future__ import annotations

import logging
import socket

log = logging.getLogger("cc_automation.kwp")

#: rows in a SELECT reply are joined by these two characters, not by a newline
ROW_SEP = "\\n"


class KwpError(Exception):
    """An `ERR ...` reply. Carries whether the server said it is retryable."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        # The one error the manual asks clients to special-case; the token is a
        # documented compatibility surface, so matching on it is intended.
        self.retryable = "retryable=1" in message


class KwpConnection:
    """One TCP connection, one command at a time.

    Not thread-safe by itself: the protocol is strictly one line in, one line
    out, so the store owns a lock around it.
    """

    def __init__(self, host: str, port: int, timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None

    def connect(self) -> None:
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._file = self._sock.makefile("rwb")

    def close(self) -> None:
        for closeable in (self._file, self._sock):
            try:
                if closeable is not None:
                    closeable.close()
            except OSError:
                pass
        self._file = self._sock = None

    def command(self, line: str) -> str:
        """Send one command, return its reply. Raises KwpError on `ERR`."""
        if self._file is None:
            self.connect()
        assert self._file is not None
        if "\n" in line:
            # Would be read as two commands, the second of them nonsense.
            raise KwpError(f"command contains a newline: {line[:80]!r}")
        try:
            self._file.write(line.encode("utf-8") + b"\n")
            self._file.flush()
            raw = self._file.readline()
        except OSError as exc:
            self.close()
            raise KwpError(f"kds connection lost: {exc}") from exc
        if not raw:
            self.close()
            raise KwpError("kds closed the connection")

        reply = raw.decode("utf-8", "replace").rstrip("\r\n")
        if reply.startswith("ERR "):
            raise KwpError(reply[4:])
        return reply

    def select(self, line: str) -> list[dict[str, str]]:
        """Run a SELECT and return its rows as column -> raw cell."""
        reply = self.command(line)
        parts = [p for p in reply.split(ROW_SEP) if p != ""]
        if not parts:
            return []
        columns = parts[0].split(",")
        rows = []
        for part in parts[1:]:
            cells = part.split(",")
            # A short row would mean a cell held a comma, which codec.py makes
            # impossible; a long one likewise. Refuse rather than mis-attribute.
            if len(cells) != len(columns):
                raise KwpError(
                    f"reply row has {len(cells)} cells, expected {len(columns)}: "
                    f"{part[:120]!r}"
                )
            rows.append(dict(zip(columns, cells)))
        return rows

    # -- transactions ------------------------------------------------------ #

    def begin(self) -> None:
        self.command("BEGIN")

    def commit(self) -> None:
        self.command("COMMIT")

    def rollback(self) -> None:
        try:
            self.command("ROLLBACK")
        except KwpError as exc:
            # Rolling back when nothing is open is not worth masking the error
            # that got us here.
            log.debug("rollback: %s", exc)
