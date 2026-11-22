"""Optional, versioned Python diagnostics from a workspace's stdio server."""

import json
import os
import subprocess
import threading
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import unquote

from cancellation import check_cancelled

from .context_pruning import mask_credentials

MAX_SOURCE_BYTES = 1_000_000
MAX_MESSAGE_BYTES = 4_000_000
MAX_DIAGNOSTICS = 12
MAX_CONTEXT_CHARS = 3200
SEVERITIES = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def _uri_key(uri: str) -> str:
    value = unquote(uri)
    return value.casefold() if os.name == "nt" else value


def _read_message(stream) -> dict:
    headers = {}
    size = 0
    while True:
        line = stream.readline(4097)
        size += len(line)
        if size > 4096 or not line:
            raise ValueError("Incomplete LSP header")
        if line == b"\r\n":
            break
        name, value = line.split(b":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise ValueError("Invalid LSP message size")
    body = stream.read(length)
    if len(body) != length:
        raise ValueError("Incomplete LSP message")
    message = json.loads(body)
    if not isinstance(message, dict):
        raise ValueError("Invalid LSP message")
    return message


def _send(process, **message):
    body = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
    process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    process.stdin.flush()


@dataclass(frozen=True, order=True)
class Diagnostic:
    level: int
    line: int
    column: int
    rule: str
    message: str


@dataclass
class Document:
    path: str
    version: int
    current: tuple[Diagnostic, ...] | None = None
    reported: tuple[Diagnostic, ...] = ()


def _one_line(value, limit):
    return mask_credentials(" ".join(value.split()))[:limit]


def _parse_diagnostics(items: list) -> tuple[Diagnostic, ...]:
    unique = set()
    for item in items[:1024]:
        try:
            start = item["range"]["start"]
            line, column, message = start["line"], start["character"], item["message"]
        except (TypeError, KeyError):
            continue
        if type(line) is not int or type(column) is not int or min(line, column) < 0 or not isinstance(message, str):
            continue
        level = item.get("severity")
        if type(level) is not int or level not in SEVERITIES:
            level = 1
        rule = item.get("code", "")
        unique.add(Diagnostic(level, line + 1, column + 1,
                              _one_line(str(rule), 80) if type(rule) in {int, str} else "",
                              _one_line(message, 180)))
    return tuple(sorted(unique)[:MAX_DIAGNOSTICS])


def diagnostic_payload(diagnostic: Diagnostic) -> dict:
    return {"line": diagnostic.line, "column": diagnostic.column, "severity": SEVERITIES[diagnostic.level],
            "rule": diagnostic.rule, "message": diagnostic.message}


def diagnostic_line(path, item):
    rule = f" [{item['rule']}]" if item["rule"] else ""
    return f"{_one_line(path, 240)}:{item['line']}:{item['column']} {item['severity']}{rule} {item['message']}"


class PythonDiagnostics:
    """A reader and worker handle pipe I/O while the turn waits on a deadline."""

    def __init__(self, root: Path, argv: list[str], timeout: float):
        self.root, self.argv, self.timeout = root, argv, timeout
        self._condition = threading.Condition()
        self._inbox = deque()
        self._outbox = {}
        self._documents: dict[str, Document] = {}
        self._active = {}
        self._waiting = set()
        self._serial = 0
        self._failed = self._failure_reported = self._stopped = False
        self._process = None
        self._thread = threading.Thread(target=self._run, name="nano-python-lsp", daemon=True)
        self._thread.start()

    def begin_turn(self):
        with self._condition:
            self._active.clear()
            self._waiting.clear()

    def update(self, path: Path):
        uri = path.as_uri()
        key = _uri_key(uri)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            text = None
            if len(raw) <= MAX_SOURCE_BYTES:
                text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeError):
            text = None
        with self._condition:
            if self._stopped:
                return
            if self._failed:
                self._active.pop(key, None)
                self._documents.pop(key, None)
                return
            self._serial += 1
            previous = self._documents.get(key)
            self._documents[key] = Document(path.relative_to(self.root).as_posix(), self._serial,
                                            reported=previous.reported if previous else ())
            self._active.pop(key, None)
            self._active[key] = None
            self._outbox[key] = (uri, self._serial, text)
            if text is None:
                self._waiting.discard(key)
            else:
                self._waiting.add(key)
            self._condition.notify_all()

    def collect(self) -> tuple[str, list[dict], bool]:
        """Wait once per edit batch and accept late results at subsequent requests."""
        deadline = monotonic() + self.timeout
        with self._condition:
            while self._waiting and not (self._stopped or self._failed):
                check_cancelled()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(0.05, remaining))
            check_cancelled()
            self._waiting.clear()
            report_failure = self._failed and not self._failure_reported
            self._failure_reported |= report_failure
            events, lines = [], []
            chars = 0
            for key in reversed(self._active):
                document = self._documents[key]
                if document.current is None:
                    continue
                items = [diagnostic_payload(item) for item in document.current]
                if document.current != document.reported:
                    events.append({"path": document.path, "version": document.version, "diagnostics": items})
                    document.reported = document.current
                for item in items:
                    line = diagnostic_line(document.path, item)
                    if len(lines) < MAX_DIAGNOSTICS and chars + len(line) + 1 <= MAX_CONTEXT_CHARS:
                        lines.append(line)
                        chars += len(line) + 1
            return "\n".join(lines), events, report_failure

    def _publish(self, params):
        if not isinstance(params, dict) or not isinstance(uri := params.get("uri"), str):
            return
        key = _uri_key(uri)
        with self._condition:
            document = self._documents.get(key)
            version, items = params.get("version"), params.get("diagnostics")
            if document is None or type(version) is not int or version != document.version or not isinstance(items, list):
                return
        diagnostics = _parse_diagnostics(items)
        with self._condition:
            if self._documents.get(key) is document:
                document.current = diagnostics
                self._waiting.discard(key)
                self._condition.notify_all()

    def _read(self, stream):
        try:
            with stream:
                while True:
                    message = _read_message(stream)
                    with self._condition:
                        if self._stopped or len(self._inbox) >= 256:
                            break
                        self._inbox.append(message)
                        self._condition.notify_all()
        except (OSError, ValueError, RecursionError):
            pass
        finally:
            with self._condition:
                self._inbox.append(None)
                self._condition.notify_all()

    def _initialize(self, process):
        root_uri = self.root.as_uri()
        _send(process, id=1, method="initialize", params={
            "processId": os.getpid(), "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "capabilities": {"textDocument": {"publishDiagnostics": {"versionSupport": True}}},
        })
        deadline = monotonic() + max(5, self.timeout)
        while True:
            with self._condition:
                remaining = max(0, deadline - monotonic())
                self._condition.wait_for(lambda: self._inbox or self._stopped, remaining)
                if self._stopped:
                    return 0
                message = self._inbox.popleft() if self._inbox else None
            if message is None or monotonic() >= deadline:
                raise ValueError("LSP initialization timed out or disconnected")
            if message.get("id") == 1 and "method" not in message:
                result = message.get("result")
                capabilities = result.get("capabilities") if isinstance(result, dict) else None
                sync = capabilities.get("textDocumentSync") if isinstance(capabilities, dict) else None
                kind = sync.get("change") if isinstance(sync, dict) else sync
                if type(kind) is not int or kind not in (1, 2):
                    raise ValueError("LSP server does not support document changes")
                _send(process, method="initialized", params={})
                return kind
            self._handle(process, message)

    def _handle(self, process, message):
        if message is None:
            raise ValueError("LSP server disconnected")
        if message.get("method") == "textDocument/publishDiagnostics":
            self._publish(message.get("params"))
        elif "method" in message and "id" in message:
            _send(process, id=message["id"], error={"code": -32601, "message": "Client method not supported"})

    def _sync(self, process, kind):
        opened = {}
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._inbox or self._outbox)
                if self._stopped:
                    return
                edit = self._outbox.pop(next(iter(self._outbox))) if self._outbox else None
                messages = list(self._inbox)
                self._inbox.clear()
            for message in messages:
                self._handle(process, message)
            if edit is not None:
                self._sync_document(process, kind, opened, *edit)

    @staticmethod
    def _sync_document(process, kind, opened, uri, version, text):
        if text is None:
            if opened.pop(uri, None) is not None:
                _send(process, method="textDocument/didClose", params={"textDocument": {"uri": uri}})
            return
        document = {"uri": uri, "version": version}
        if uri in opened:
            change = {"text": text}
            if kind == 2:
                change["range"] = {"start": {"line": 0, "character": 0}, "end": opened[uri]}
            _send(process, method="textDocument/didChange",
                  params={"textDocument": document, "contentChanges": [change]})
        else:
            _send(process, method="textDocument/didOpen",
                  params={"textDocument": {**document, "languageId": "python", "text": text}})
        last_line = text.rpartition("\n")[2]
        opened[uri] = {"line": text.count("\n"), "character": len(last_line.encode("utf-16-le")) // 2}

    def _run(self):
        process = reader = None
        try:
            process = subprocess.Popen(
                self.argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._condition:
                self._process = process
                self._condition.notify_all()
                if self._stopped:
                    return
            reader = threading.Thread(target=self._read, args=(process.stdout,), daemon=True)
            reader.start()
            if kind := self._initialize(process):
                self._sync(process, kind)
        except (OSError, ValueError):
            with self._condition:
                self._failed = not self._stopped
                self._outbox.clear()
                self._condition.notify_all()
        finally:
            if process is not None:
                self._kill(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                if reader is not None:
                    reader.join(timeout=0.25)
                with suppress(OSError):
                    process.stdin.close()
                if reader is None:
                    process.stdout.close()

    @staticmethod
    def _kill(process):
        if process is not None and process.poll() is None:
            with suppress(OSError):
                process.kill()

    def close(self):
        with self._condition:
            self._stopped = True
            process = self._process
            self._condition.notify_all()
        # Terminating the server also releases a worker blocked on a full pipe.
        self._kill(process)
        self._thread.join(timeout=1.5)
