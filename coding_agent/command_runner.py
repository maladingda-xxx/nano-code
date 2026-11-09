"""Run an approved command, stream output, and stop its process group on cancellation."""

import codecs
import locale
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path

from agent_core import ToolResult


@contextmanager
def _start_process(argv, cwd, stdin, stdout, stderr):
    if os.name == "nt":
        from windows_job import start_process

        with start_process(argv, cwd, stdin, stdout, stderr) as poll:
            yield poll
        return

    with subprocess.Popen(
        argv, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True,
    ) as process:
        try:
            yield process.poll
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class _OutputReader:
    def __init__(self, stream, name: str, emit: Callable[[str, str], None]):
        self.stream = stream
        self.name = name
        self.emit = emit
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def _publish(self, data: bytes, *, final: bool = False) -> None:
        buffered = self.decoder.getstate()[0]
        try:
            text = self.decoder.decode(data, final=final)
        except UnicodeDecodeError:
            encoding = locale.getpreferredencoding(False)
            self.decoder = codecs.getincrementaldecoder(encoding)("replace")
            text = self.decoder.decode(buffered + data, final=final)
        if text:
            self.emit(self.name, text)

    def pump(self) -> bool:
        data = self.stream.read(8192)
        if data:
            self._publish(data)
        return bool(data)

    def finish(self) -> None:
        # Read only bytes already present when the command ended.
        remaining = os.fstat(self.stream.fileno()).st_size - self.stream.tell()
        while remaining > 0:
            data = self.stream.read(min(8192, remaining))
            if not data:
                break
            self._publish(data)
            remaining -= len(data)
        self._publish(b"", final=True)


def _output_tail(stream) -> tuple[str, bool]:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - 14_000))
    data = stream.read()
    try:
        output = data.decode("utf-8")
    except UnicodeDecodeError:
        output = data.decode(locale.getpreferredencoding(False), errors="replace")
    truncated = size > len(data) or len(output) > 3500
    return ("…（前文已截断）\n" + output[-3500:] if truncated else output), truncated


def _open_output_reader(path):
    # Match NamedTemporaryFile's delete sharing on Windows. A killed descendant
    # may still be closing an inherited handle when the parent returns.
    def opener(name, flags):
        return os.open(name, flags | getattr(os, "O_TEMPORARY", 0))

    return open(path, "rb", buffering=0, opener=opener)


def run_process(
    argv: list[str], cwd: Path, timeout: float,
    on_output: Callable[[str, str], None] | None = None,
) -> ToolResult:
    names = ("stdout", "stderr")
    exit_code = None
    status = "ok"
    with ExitStack() as stack:
        writers = [
            stack.enter_context(tempfile.NamedTemporaryFile(prefix=f"nano-code-{name}-"))
            for name in names
        ]
        stdin = stack.enter_context(open(os.devnull, "rb"))
        readers = [
            _OutputReader(stack.enter_context(_open_output_reader(writer.name)), name, on_output)
            for name, writer in zip(names, writers)
        ] if on_output else []
        try:
            with _start_process(argv, cwd, stdin, *writers) as poll:
                deadline = time.monotonic() + timeout
                while (exit_code := poll()) is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        status = "timeout"
                        break
                    activity = [reader.pump() for reader in readers]
                    if not any(activity):
                        time.sleep(min(0.05, remaining))
            for reader in readers:
                reader.finish()
        except KeyboardInterrupt:
            status = "interrupted"
            exit_code = None
        (stdout, stdout_truncated), (stderr, stderr_truncated) = [
            _output_tail(writer) for writer in writers
        ]

    if status == "timeout":
        heading = f"命令执行超过 {timeout:g} 秒，已停止。"
    elif status == "interrupted":
        heading = "用户取消了命令。请先检查工作区，再决定是否重试。"
    else:
        status = "ok" if exit_code == 0 else "failed"
        heading = f"退出码: {exit_code}"
    parts = [heading]
    for name, output in zip(names, (stdout, stderr)):
        if output:
            parts.append(f"[{name}]\n{output.rstrip()}")
    return ToolResult(
        "\n".join(parts), status=status, exit_code=exit_code, stdout=stdout, stderr=stderr,
        truncated=stdout_truncated or stderr_truncated,
    )
