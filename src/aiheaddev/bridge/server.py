"""Private newline-JSON asyncio server. stdout is owned exclusively by the codec."""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Callable

from .protocol import (BridgeError, MAX_MESSAGE_BYTES, PROTOCOL, decode, encode,
                       validate_event, validate_request, validate_response)
from .runtime import LiveRuntime, logs
from .settings import SettingsService, catalog_get, folders_list, folders_test
from .demo import DemoRuntime, DemoSettings

MAX_PENDING = 32
MAX_QUEUED = 256


class BridgeServer:
    """Serve one native client. Dispatched writes are never cancelled or retried."""

    def __init__(self, reader: Any, writer: Any, *, runtime_factory: Callable[..., Any] = LiveRuntime,
                 settings_factory: Callable[..., Any] = SettingsService, poll_interval: float = 3.0) -> None:
        self.reader, self.writer = reader, writer
        self.runtime_factory, self.settings_factory = runtime_factory, settings_factory
        self.poll_interval = poll_interval
        self.runtime: Any = None
        self.settings: Any = None
        self.demo = False
        self._initialized = False
        self._closed = False
        self._broken = False
        self._revision = 0
        self._queue: deque[list[Any]] = deque()
        self._coalesced: dict[Any, list[Any]] = {}
        self._ready = asyncio.Event()
        self._output_failed = asyncio.Event()
        self._space = asyncio.Event()
        self._space.set()
        self._pending: dict[Any, asyncio.Task] = {}
        self._launch_pending = False
        self._poll: asyncio.Task | None = None
        self._refresh: asyncio.Task | None = None
        self._deferred: list[tuple[str, dict[str, Any]]] | None = None

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._broken:
            return
        if self._deferred is not None and event in {"inventory", "pane_frame"}:
            self._deferred.append((event, payload))
            # Deferred rendering is coalescible; bound it even during a slow selection.
            if len(self._deferred) > 128:
                self._deferred.pop(0)
            return
        if event == "inventory":
            key: Any = ("inventory",)
        elif event == "pane_frame":
            target = payload.get("target", {})
            key = (event, target.get("socket_path"), target.get("workspace_id"), target.get("tab_id"), target.get("pane_id"))
        else:
            key = None
        try:
            validate_event({"event": event, "revision": 0, "payload": payload})
        except BridgeError:
            return
        node = self._coalesced.get(key) if key is not None else None
        if node is not None:
            node[2] = payload
        else:
            # Up to 64 pane previews plus inventory; obsolete output can be dropped.
            if key is not None and len(self._coalesced) >= 128:
                return
            node = ["event", event, payload, key]
            self._queue.append(node)
            if key is not None:
                self._coalesced[key] = node
        self._ready.set()

    async def _response(self, value: dict[str, Any]) -> None:
        while len(self._queue) >= MAX_QUEUED and not self._broken:
            self._space.clear()
            await self._space.wait()
        if not self._broken:
            self._queue.append(["response", value])
            self._ready.set()

    async def _write_loop(self) -> None:
        try:
            while True:
                if not self._queue:
                    if self._closed:
                        return
                    self._ready.clear()
                    await self._ready.wait()
                    continue
                node = self._queue.popleft()
                self._space.set()
                if node[0] == "event":
                    _, event, payload, key = node
                    if key is not None:
                        self._coalesced.pop(key, None)
                    self._revision += 1
                    message = {"event": event, "revision": self._revision, "payload": payload}
                else:
                    message = node[1]
                    try:
                        validate_response(message)
                    except BridgeError:
                        message = {"id": message["id"], "error": {"code": "internal_error", "message": "Bridge returned an invalid response"}}
                try:
                    wire = encode(message)
                except BridgeError:
                    # An unencodable read-only event is discarded; a response gets a safe error.
                    if node[0] == "event":
                        continue
                    wire = encode({"id": message["id"], "error": {"code": "message_too_large", "message": "Response exceeds protocol limits"}})
                self.writer.write(wire)
                await self.writer.drain()
        except (BrokenPipeError, ConnectionError, OSError):
            self._broken = True
            self._output_failed.set()
            self._queue.clear()
            self._coalesced.clear()
            self._space.set()

    async def _poll_loop(self) -> None:
        while True:
            self._schedule_refresh()
            await asyncio.sleep(self.poll_interval)

    def _schedule_refresh(self) -> None:
        if self._refresh is None or self._refresh.done():
            self._refresh = asyncio.create_task(self._safe_refresh())

    async def _safe_refresh(self) -> None:
        try:
            await self.runtime.refresh()
        except Exception:
            # A failed poll is not evidence of a successful operation.
            pass

    async def _mutation(self, request_id: Any, method: str, params: dict[str, Any], launch: bool) -> None:
        try:
            if method in {"reply.submit", "terminal.input"}:
                outcome, message, target = await self.runtime.send(method, params)
            elif method.startswith("launch."):
                outcome, message, target = await self.runtime.launch(method, params)
            elif method == "aihead.open":
                outcome, message, target = await self.runtime.open_aihead(params)
            else:
                outcome, message, target = await self.runtime.delete(params)
        except BridgeError as exc:
            outcome, message, target = exc.outcome or "rejected", exc.message, None
        except Exception:
            outcome, message, target = "uncertain", "Operation failed; inspect Herdr before retrying", None
        event = "reply_result" if method in {"reply.submit", "terminal.input"} else "operation_status"
        payload: dict[str, Any] = {"request_id": request_id, "outcome": outcome, "message": message}
        if target is not None:
            payload["lease" if event == "reply_result" else "target"] = target
        try:
            validate_event({"event": event, "revision": 0, "payload": payload})
        except BridgeError:
            # A malformed backend result must not silently lose a completion.
            outcome = "uncertain"
            payload = {"request_id": request_id, "outcome": outcome,
                       "message": "Operation returned an invalid result; inspect Herdr before retrying"}
        self.emit(event, payload)
        self._pending.pop(request_id, None)
        if launch:
            self._launch_pending = False
        if outcome == "accepted" and event == "operation_status":
            self._schedule_refresh()

    async def _handle(self, request: dict[str, Any]) -> bool:
        request_id, method, params = request["id"], request["method"], request["params"]
        if method != "initialize" and not self._initialized:
            raise BridgeError("not_initialized", "Initialize the bridge first")
        if method == "initialize":
            if self._initialized:
                raise BridgeError("already_initialized", "Bridge is already initialized")
            self.demo = params.get("demo", False)
            self.settings = DemoSettings() if self.demo else self.settings_factory()
            self.runtime = DemoRuntime(self.emit) if self.demo else self.runtime_factory(self.emit, session=params.get("session"))
            self._initialized = True
            await self._response({"id": request_id, "result": {"protocol": PROTOCOL, "capabilities": []}})
            self._poll = asyncio.create_task(self._poll_loop())
            return True
        if method == "shutdown":
            await self._response({"id": request_id, "result": {}})
            return False
        if method in {"reply.submit", "terminal.input", "workspace.delete", "aihead.open"} or method.startswith("launch."):
            if request_id in self._pending:
                raise BridgeError("duplicate_request", "Request id is already in flight")
            if len(self._pending) >= MAX_PENDING:
                raise BridgeError("too_many_operations", "Wait for pending operations")
            launch = method.startswith("launch.") or method == "aihead.open"
            if launch and self._launch_pending:
                raise BridgeError("operation_in_progress", "A launch or AiHead operation is already in progress")
            if launch:
                self._launch_pending = True
            await self._response({"id": request_id, "result": {"pending": True}})
            self._pending[request_id] = asyncio.create_task(self._mutation(request_id, method, params, launch))
            return True
        if method == "inventory.refresh":
            await self._response({"id": request_id, "result": {}})
            self._schedule_refresh()
            return True
        if method == "selection.set":
            self._deferred = []
            try:
                await self.runtime.select(params["target"])
                await self._response({"id": request_id, "result": {}})
            finally:
                deferred, self._deferred = self._deferred, None
                for event, payload in deferred or []:
                    self.emit(event, payload)
            return True
        if method == "catalog.get":
            result = await asyncio.to_thread(catalog_get)
        elif method == "project.open":
            result = await asyncio.to_thread(self.settings.project_open, params["path"])
        elif method in {"folders.list", "folders.test"}:
            if self.demo:
                result = {"path": params["path"], "available": False}
                if method == "folders.list":
                    result["directories"] = []
            else:
                fn = folders_list if method == "folders.list" else folders_test
                result = await asyncio.to_thread(fn, params["path"], project=params.get("project"))
        elif method == "settings.validate" and params.get("discard"):
            await asyncio.to_thread(self.settings.discard, params["token"], revision=params["revision"])
            result = {}
        elif method in {"settings.validate", "settings.save"}:
            fn = self.settings.validate if method == "settings.validate" else self.settings.save
            result = await asyncio.to_thread(fn, params["token"], params["sections"], revision=params["revision"])
        elif method == "logs.get":
            result = {"records": []} if self.demo else logs(params.get("limit", 200))
        else:
            raise BridgeError("unknown_method", "Method is not allowed")
        await self._response({"id": request_id, "result": result})
        return True

    async def _readline_or_disconnect(self) -> bytes:
        read = asyncio.create_task(self.reader.readline())
        failed = asyncio.create_task(self._output_failed.wait())
        try:
            done, _ = await asyncio.wait((read, failed), return_when=asyncio.FIRST_COMPLETED)
            if failed in done and self._output_failed.is_set():
                return b""
            return read.result()
        finally:
            for task in (read, failed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(read, failed, return_exceptions=True)

    async def serve(self) -> None:
        writer_task = asyncio.create_task(self._write_loop())
        try:
            while not self._broken:
                try:
                    line = await self._readline_or_disconnect()
                except (ValueError, asyncio.LimitOverrunError, OSError):
                    break
                if not line:
                    break
                if len(line) > MAX_MESSAGE_BYTES:
                    break
                request_id = None
                try:
                    value = decode(line)
                    candidate = value.get("id")
                    if (type(candidate) is int and 0 <= candidate <= 2**53 - 1) or (isinstance(candidate, str) and candidate.strip() and len(candidate) <= 4096):
                        request_id = candidate
                    request = validate_request(value)
                    if not await self._handle(request):
                        break
                except BridgeError as exc:
                    if request_id is None:
                        break
                    await self._response({"id": request_id, "error": exc.as_dict()})
                except Exception:
                    if request_id is None:
                        break
                    await self._response({"id": request_id, "error": {"code": "internal_error", "message": "Bridge operation failed"}})
        finally:
            if self._poll:
                self._poll.cancel()
                await asyncio.gather(self._poll, return_exceptions=True)
            if self._refresh:
                self._refresh.cancel()
                await asyncio.gather(self._refresh, return_exceptions=True)
            # Once dispatched, mutation tasks are allowed to finish even when the reader
            # disconnects. Never cancel/retry them or close Herdr workspaces.
            if self._pending:
                await asyncio.gather(*list(self._pending.values()), return_exceptions=True)
            if self.runtime:
                await self.runtime.close()
            self.settings = None
            self._closed = True
            self._ready.set()
            await writer_task


async def serve(reader: Any, writer: Any, **kwargs: Any) -> None:
    await BridgeServer(reader, writer, **kwargs).serve()
