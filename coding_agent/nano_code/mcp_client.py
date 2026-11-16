"""MCP tools over stdio and Streamable HTTP, with bounded requests and cleanup."""

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import httpx

from cancellation import check_cancelled
from . import __version__


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, name, settings, root):
        self.name, self.settings, self.root = name, settings, Path(root)
        self.process = None
        self.http = None
        self.session_id = None
        self.serial = 0
        self.inbox = queue.Queue(maxsize=2048)
        self.timeout = min(120, max(1, float(settings.get("timeout", 30))))
        self.protocol = "2025-06-18"
        self.tools = []

    def connect(self):
        try:
            if "url" in self.settings:
                headers = {"Accept": "application/json, text/event-stream", **self.settings.get("headers", {})}
                for key, variable in self.settings.get("headers_env", {}).items():
                    if variable not in os.environ:
                        raise MCPError(f"MCP {self.name}: 缺少环境变量 {variable}")
                    headers[key] = os.environ[variable]
                self.http = httpx.Client(timeout=self.timeout, headers=headers, follow_redirects=False)
            else:
                argv = [self.settings["command"], *self.settings.get("args", [])]
                if not all(isinstance(arg, str) for arg in argv) or Path(argv[0]).suffix.lower() in {".cmd", ".bat"}:
                    raise MCPError("MCP stdio 需要可执行程序；Node 服务请使用 node server.js。")
                self.process = subprocess.Popen(
                    argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                    env={**os.environ, **self.settings.get("env", {})},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    start_new_session=os.name != "nt",
                )
                threading.Thread(target=self._read, args=(self.process.stdout,), daemon=True).start()
            response = self.request("initialize", {
                "protocolVersion": self.protocol, "capabilities": {},
                "clientInfo": {"name": "nano-code", "version": __version__},
            })
            self.protocol = response.get("protocolVersion", self.protocol)
            self.notify("notifications/initialized")
            cursor = None
            for _ in range(100):
                page = self.request("tools/list", {"cursor": cursor} if cursor else {})
                self.tools.extend(page.get("tools", []))
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            return self.tools
        except BaseException:
            self.close()
            raise

    def _read(self, stream):
        try:
            while True:
                line = stream.readline(2_000_001)
                if not line:
                    break
                if len(line) > 2_000_000:
                    self.inbox.put_nowait({"_error": "MCP message exceeds 2 MB"})
                    break
                try:
                    self.inbox.put_nowait(json.loads(line))
                except ValueError:
                    self.inbox.put_nowait({"_error": "MCP stdout contains invalid JSON"})
                    break
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                self.inbox.put_nowait({"_error": "MCP server disconnected"})
            except queue.Full:
                pass

    def _http_send(self, payload):
        headers = {"MCP-Protocol-Version": self.protocol}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        with self.http.stream("POST", self.settings["url"], json=payload, headers=headers) as response:
            response.raise_for_status()
            self.session_id = response.headers.get("Mcp-Session-Id", self.session_id)
            if response.status_code in {202, 204}:
                return {}
            if "text/event-stream" in response.headers.get("content-type", ""):
                data = []
                for line in response.iter_lines():
                    check_cancelled()
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        event = json.loads("\n".join(data))
                        data = []
                        if event.get("id") == payload.get("id"):
                            return event
                raise MCPError("MCP HTTP stream ended without a result")
            body = bytearray()
            for chunk in response.iter_bytes():
                check_cancelled()
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise MCPError("MCP response exceeds 2 MB")
            return json.loads(body) if body else {}

    def _send(self, payload):
        if self.http:
            return self._http_send(payload)
        if not self.process or self.process.poll() is not None:
            raise MCPError("MCP server is not connected")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params):
        self.serial += 1
        request_id = self.serial
        try:
            message = self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            if not self.http:
                deadline = time.monotonic() + self.timeout
                while True:
                    check_cancelled()
                    if time.monotonic() >= deadline:
                        raise MCPError(f"MCP {self.name}: {method} timed out")
                    try:
                        message = self.inbox.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if "_error" in message:
                        raise MCPError(message["_error"])
                    if "method" in message and "id" in message:
                        # Sampling and elicitation are not implicitly authorized.
                        self._send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Client method not supported"}})
                    elif message.get("id") == request_id:
                        break
            if not isinstance(message, dict) or message.get("id") != request_id:
                raise MCPError("MCP response ID mismatch")
            if "error" in message:
                raise MCPError(str(message["error"])[:1000])
            return message.get("result", {})
        except (httpx.HTTPError, OSError, ValueError) as error:
            raise MCPError(f"MCP {self.name}: {type(error).__name__}") from error

    def call(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self):
        if self.http:
            if self.session_id:
                try:
                    self.http.delete(self.settings["url"], headers={"Mcp-Session-Id": self.session_id}, timeout=2)
                except httpx.HTTPError:
                    pass
            self.http.close()
            self.http = None
        if self.process:
            process, self.process = self.process, None
            if process.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        process.terminate()
                else:
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()


def tool_name(server, name):
    return "mcp__" + re.sub(r"[^a-zA-Z0-9_-]", "_", server)[:20] + "__" + re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:35]
