from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import anyio.to_thread
import pytest

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import _threadpool_limiter, current_threadpool_limiter
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Scope
from tests.types import TestClientFactory

pytestmark = pytest.mark.anyio


async def wait_for(predicate: object, timeout: float = 5.0) -> None:
    """Poll a predicate from the event loop, so the same test runs on asyncio and trio."""
    deadline = anyio.current_time() + timeout
    while not bool(predicate()):
        if anyio.current_time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await anyio.sleep(0.01)


def make_scope(path: str = "/") -> Scope:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [],
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
    }


async def receive() -> Message:
    return {"type": "http.disconnect"}


async def call_http(app: ASGIApp, path: str = "/") -> list[Message]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await app(make_scope(path), receive, send)
    return messages


class SyncGate:
    """Allows worker threads to block until the event loop releases them."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.running = 0
        self.max_running = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self._event.wait(10)
        with self._lock:
            self.running -= 1

    def release(self) -> None:
        self._event.set()


def make_gated_endpoint(gate: SyncGate) -> object:
    def endpoint(request: Request) -> Response:
        gate.enter()
        return PlainTextResponse("ok")

    return endpoint


def test_threadpool_capacity_creates_own_limiter() -> None:
    app = Starlette(threadpool_capacity=1)
    assert app.threadpool_limiter is not None
    assert app.threadpool_limiter.total_tokens == 1

    default_app = Starlette()
    assert default_app.threadpool_limiter is None


def test_invalid_threadpool_capacity() -> None:
    with pytest.raises(ValueError, match="threadpool_capacity"):
        Starlette(threadpool_capacity=0)
    with pytest.raises(ValueError, match="threadpool_capacity"):
        Starlette(threadpool_capacity=-1)


async def test_sync_routes_share_application_capacity() -> None:
    gate = SyncGate()
    app = Starlette(
        routes=[Route("/", make_gated_endpoint(gate))],
        threadpool_capacity=1,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(call_http, app, "/")
        task_group.start_soon(call_http, app, "/")
        task_group.start_soon(call_http, app, "/")

        await wait_for(lambda: gate.running == 1 and limiter.available_tokens == 0)
        # Only one of the three requests may run at a time.
        await anyio.sleep(0.1)
        assert gate.running == 1
        assert gate.max_running == 1

        gate.release()

    assert gate.max_running == 1
    assert limiter.available_tokens == 1
    assert _threadpool_limiter.get() is None


async def test_applications_do_not_wait_for_each_other() -> None:
    heavy_gate = SyncGate()
    light_gate = SyncGate()
    heavy_app = Starlette(
        routes=[Route("/", make_gated_endpoint(heavy_gate))],
        threadpool_capacity=1,
    )
    light_app = Starlette(
        routes=[Route("/", make_gated_endpoint(light_gate))],
        threadpool_capacity=2,
    )

    async with anyio.create_task_group() as task_group:
        # Fill the heavy application's single slot, and queue another request.
        task_group.start_soon(call_http, heavy_app)
        task_group.start_soon(call_http, heavy_app)
        await wait_for(lambda: heavy_gate.running == 1)

        # The other application has an independent quota, so it is not queued.
        task_group.start_soon(call_http, light_app)
        await wait_for(lambda: light_gate.running == 1)
        assert heavy_gate.running == 1

        heavy_gate.release()
        light_gate.release()

    assert heavy_gate.max_running == 1
    assert light_gate.max_running == 1
    assert heavy_app.threadpool_limiter is not light_app.threadpool_limiter


def make_limiter_reporting_endpoint() -> object:
    def endpoint(request: Request) -> Response:
        limiter = current_threadpool_limiter()
        return PlainTextResponse("none" if limiter is None else str(id(limiter)))

    return endpoint


async def test_mounted_application_without_capacity_inherits_parent_limiter() -> None:
    gate = SyncGate()
    child = Starlette(routes=[Route("/", make_gated_endpoint(gate))])
    parent = Starlette(
        routes=[
            Mount("/child", app=child),
            Route("/", make_limiter_reporting_endpoint()),
        ],
        threadpool_capacity=1,
    )
    assert child.threadpool_limiter is None
    limiter = parent.threadpool_limiter
    assert limiter is not None

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(call_http, parent, "/child/")
        await wait_for(lambda: gate.running == 1 and limiter.available_tokens == 0)

        # The mounted application shares the parent's single token, so a
        # request on the parent itself has to wait behind the child request.
        blocked_messages: list[Message] = []

        async def blocked_call() -> None:
            blocked_messages.extend(await call_http(parent, "/"))

        task_group.start_soon(blocked_call)
        await anyio.sleep(0.1)
        assert blocked_messages == []
        assert gate.running == 1

        gate.release()

    messages = await call_http(parent, "/")
    assert messages[-1]["body"] == str(id(limiter)).encode()


async def test_mounted_application_with_own_capacity_uses_own_limiter() -> None:
    parent_gate = SyncGate()
    child_gate = SyncGate()
    child = Starlette(
        routes=[Route("/", make_gated_endpoint(child_gate))],
        threadpool_capacity=1,
    )
    parent = Starlette(
        routes=[
            Mount("/child", app=child),
            Route("/", make_gated_endpoint(parent_gate)),
        ],
        threadpool_capacity=1,
    )
    assert parent.threadpool_limiter is not child.threadpool_limiter

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(call_http, parent, "/")
        await wait_for(lambda: parent_gate.running == 1)

        # The child has its own token, so it runs while the parent is saturated.
        task_group.start_soon(call_http, parent, "/child/")
        await wait_for(lambda: child_gate.running == 1)
        assert parent_gate.running == 1

        parent_gate.release()
        child_gate.release()

    assert parent_gate.max_running == 1
    assert child_gate.max_running == 1
    # The parent limiter must not have been replaced by the child's binding.
    assert _threadpool_limiter.get() is None


async def test_default_behavior_without_threadpool_capacity() -> None:
    gate = SyncGate()
    app = Starlette(routes=[Route("/", make_gated_endpoint(gate))])
    assert app.threadpool_limiter is None
    default_limiter = anyio.to_thread.current_default_thread_limiter()
    before = default_limiter.available_tokens

    async with anyio.create_task_group() as task_group:
        for _ in range(3):
            task_group.start_soon(call_http, app)
        await wait_for(lambda: gate.running == 3)
        gate.release()

    assert default_limiter.available_tokens == before
    assert _threadpool_limiter.get() is None


def failing_endpoint(request: Request) -> Response:
    raise RuntimeError("boom")


async def test_tokens_and_context_recover_after_error() -> None:
    app = Starlette(
        routes=[Route("/", failing_endpoint)],
        threadpool_capacity=1,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    with pytest.raises(RuntimeError, match="boom"):
        await call_http(app)

    assert limiter.available_tokens == 1
    assert _threadpool_limiter.get() is None


async def test_tokens_recover_after_cancellation() -> None:
    gate = SyncGate()
    app = Starlette(
        routes=[Route("/", make_gated_endpoint(gate))],
        threadpool_capacity=1,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(call_http, app)
        await wait_for(lambda: gate.running == 1 and limiter.available_tokens == 0)
        task_group.cancel_scope.cancel()
        gate.release()

    # AnyIO lets the running thread finish and always returns the token.
    await wait_for(lambda: limiter.available_tokens == 1)
    assert _threadpool_limiter.get() is None


async def test_sync_streaming_response_shares_application_capacity() -> None:
    gate = SyncGate()

    def sync_iter() -> Iterator[bytes]:
        gate.enter()
        yield b"hello"

    app = Starlette(
        routes=[Route("/", lambda request: StreamingResponse(sync_iter()))],
        threadpool_capacity=1,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(call_http, app)
        await wait_for(lambda: gate.running == 1 and limiter.available_tokens == 0)
        gate.release()

    messages = await call_http(app)
    assert messages[-1]["body"] == b""
    assert limiter.available_tokens == 1


async def test_sync_background_task_shares_application_capacity() -> None:
    gate = SyncGate()
    ran = threading.Event()

    def background_work() -> None:
        ran.set()
        gate.enter()

    def endpoint(request: Request) -> Response:
        return PlainTextResponse("ok", background=BackgroundTask(background_work))

    app = Starlette(
        routes=[Route("/", endpoint)],
        threadpool_capacity=1,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(call_http, app)
        await wait_for(lambda: ran.is_set() and gate.running == 1 and limiter.available_tokens == 0)
        gate.release()

    assert limiter.available_tokens == 1


async def test_async_endpoint_does_not_consume_capacity() -> None:
    async def async_endpoint(request: Request) -> Response:
        # Async work runs on the event loop and must not touch the limiter.
        assert current_threadpool_limiter() is app.threadpool_limiter
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/", async_endpoint)],
        threadpool_capacity=2,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    messages = await call_http(app)
    assert messages[-1]["body"] == b"ok"
    assert limiter.available_tokens == 2


@asynccontextmanager
async def unused_lifespan(app: Starlette) -> AsyncIterator[None]:
    yield


def test_lifespan_scope_does_not_bind_limiter(test_client_factory: TestClientFactory) -> None:
    app = Starlette(lifespan=unused_lifespan, threadpool_capacity=1)
    with test_client_factory(app):
        assert _threadpool_limiter.get() is None


async def test_file_response_uses_application_capacity(tmp_path: Path) -> None:
    file_path = tmp_path / "example.txt"
    file_path.write_text("payload")
    gate = SyncGate()

    def endpoint(request: Request) -> FileResponse:
        assert current_threadpool_limiter() is app.threadpool_limiter
        return FileResponse(file_path)

    app = Starlette(
        routes=[Route("/", endpoint)],
        threadpool_capacity=1,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    # Block the FileResponse `os.stat` worker call and observe the app limiter.
    original_stat = os.stat

    def blocking_stat(path: object, *args: object, **kwargs: object) -> object:
        gate.enter()
        return original_stat(path, *args, **kwargs)

    os.stat = blocking_stat  # type: ignore[assignment]
    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(call_http, app)
            await wait_for(lambda: gate.running == 1 and limiter.borrowed_tokens == 1)
            gate.release()
    finally:
        os.stat = original_stat  # type: ignore[assignment]

    assert limiter.borrowed_tokens == 0
    assert limiter.available_tokens == 1


async def test_static_files_mounted_app_inherits_parent_capacity(tmp_path: Path) -> None:
    (tmp_path / "example.txt").write_text("static")
    gate = SyncGate()

    static = StaticFiles(directory=tmp_path, check_dir=False)
    app = Starlette(
        routes=[Mount("/static", app=static)],
        threadpool_capacity=1,
    )
    limiter = app.threadpool_limiter
    assert limiter is not None

    # Block the StaticFiles path lookup worker call and observe the parent limiter.
    original_lookup_path = static.lookup_path

    def blocking_lookup_path(path: str) -> object:
        gate.enter()
        return original_lookup_path(path)

    static.lookup_path = blocking_lookup_path  # type: ignore[method-assign]
    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(call_http, app, "/static/example.txt")
            await wait_for(lambda: gate.running == 1 and limiter.borrowed_tokens == 1)
            gate.release()
    finally:
        static.lookup_path = original_lookup_path  # type: ignore[method-assign]

    assert limiter.borrowed_tokens == 0
    assert limiter.available_tokens == 1


async def test_concurrent_requests_to_other_apps_keep_context_isolated() -> None:
    first_gate = SyncGate()
    second_gate = SyncGate()
    first_app = Starlette(
        routes=[Route("/", make_gated_endpoint(first_gate))],
        threadpool_capacity=1,
    )
    second_app = Starlette(
        routes=[Route("/", make_gated_endpoint(second_gate))],
        threadpool_capacity=1,
    )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(call_http, first_app)
        task_group.start_soon(call_http, second_app)
        await wait_for(lambda: first_gate.running == 1 and second_gate.running == 1)

        # Both applications hold their own single token at the same time.
        assert first_app.threadpool_limiter is not None
        assert second_app.threadpool_limiter is not None
        assert first_app.threadpool_limiter.available_tokens == 0
        assert second_app.threadpool_limiter.available_tokens == 0

        first_gate.release()
        second_gate.release()

    assert first_app.threadpool_limiter.available_tokens == 1
    assert second_app.threadpool_limiter.available_tokens == 1
    assert _threadpool_limiter.get() is None
