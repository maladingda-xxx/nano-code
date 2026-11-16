"""Version 1 newline-delimited JSON-RPC for external terminal/IDE clients."""

import concurrent.futures
import json
import sys
import threading

from . import __version__


def serve(session, input_stream=None, output_stream=None):
    source, sink = input_stream or sys.stdin, output_stream or sys.stdout
    lock = threading.Lock()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    active = None

    def write(message):
        with lock:
            sink.write(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n")
            sink.flush()

    session.events = lambda event: write({"method": "event", "params": event})
    session.permissions.ask = None

    def finish(future, request_id):
        try:
            write({"id": request_id, "result": future.result()})
        except BaseException as error:
            write({"id": request_id, "error": {"code": -32000, "message": str(error) or "Cancelled"}})

    try:
        for line in source:
            request_id = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise ValueError("Expected a JSON-RPC 2.0 object")
                request_id = request.get("id")
                method, params = request.get("method"), request.get("params", {})
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                if method == "initialize":
                    result = {"protocol_version": 1, "version": __version__, "session_id": session.uid,
                              "capabilities": ["run", "cancel", "status", "resume", "fork", "shutdown"]}
                elif method == "run":
                    if active and not active.done():
                        raise ValueError("A turn is already running")
                    active = executor.submit(session.run, params["prompt"])
                    active.add_done_callback(lambda f, rid=request_id: finish(f, rid))
                    continue
                elif method == "cancel":
                    session.cancel()
                    if active:
                        active.cancel()
                    result = {"cancel_requested": True}
                elif method == "status":
                    result = {"busy": bool(active and not active.done()), "session_id": session.uid, "last_result": session.last_result}
                elif method in {"resume", "fork"}:
                    if active and not active.done():
                        raise ValueError("Wait for the running turn before changing sessions")
                    result = {"session_id": session.resume(params.get("session_id"), fork_session=method == "fork")}
                elif method == "shutdown":
                    session.cancel()
                    write({"id": request_id, "result": {"shutting_down": True}})
                    break
                else:
                    write({"id": request_id, "error": {"code": -32601, "message": "Method not found"}})
                    continue
                if "id" in request:
                    write({"id": request_id, "result": result})
            except (ValueError, TypeError, KeyError) as error:
                write({"id": request_id, "error": {"code": -32602, "message": str(error)}})
    finally:
        session.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
    return 0
