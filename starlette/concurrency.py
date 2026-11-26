from __future__ import annotations

import contextlib
import contextvars
import functools
import warnings
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable, Iterator
from typing import ParamSpec, TypeVar

import anyio.to_thread
from anyio import CapacityLimiter

from starlette.exceptions import StarletteDeprecationWarning

P = ParamSpec("P")
T = TypeVar("T")

# Holds the capacity limiter of the application that is currently handling the
# request. It is set around the whole request handling, so every piece of
# synchronous work that Starlette offloads to a thread shares the quota of
# the application the request belongs to. When it is not set, AnyIO falls
# back to its default thread limiter.
_threadpool_limiter: contextvars.ContextVar[CapacityLimiter | None] = contextvars.ContextVar(
    "starlette_threadpool_limiter", default=None
)


@contextlib.contextmanager
def use_threadpool_limiter(limiter: CapacityLimiter) -> Iterator[None]:
    """Bind synchronous threadpool work to an application specific limiter.

    The previous binding is always restored when the context exits, including
    on errors and cancellations, so requests cannot leak an application
    limiter into unrelated requests running on the same event loop.
    """
    token = _threadpool_limiter.set(limiter)
    try:
        yield
    finally:
        _threadpool_limiter.reset(token)


def current_threadpool_limiter() -> CapacityLimiter | None:
    """Return the limiter bound to the request being handled, if any.

    A `None` result means synchronous work should use AnyIO's default thread
    limiter, which is the behavior of applications without
    `threadpool_capacity`.
    """
    return _threadpool_limiter.get()


async def run_until_first_complete(*args: tuple[Callable, dict]) -> None:  # type: ignore[type-arg]
    warnings.warn(
        "run_until_first_complete is deprecated and will be removed in a future version.",
        StarletteDeprecationWarning,
    )

    async with anyio.create_task_group() as task_group:

        async def run(func: Callable[[], Coroutine]) -> None:  # type: ignore[type-arg]
            await func()
            task_group.cancel_scope.cancel()

        for func, kwargs in args:
            task_group.start_soon(run, functools.partial(func, **kwargs))


async def run_in_threadpool(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    func = functools.partial(func, *args, **kwargs)
    return await anyio.to_thread.run_sync(func, limiter=current_threadpool_limiter())


class _StopIteration(Exception):
    pass


def _next(iterator: Iterator[T]) -> T:
    # We can't raise `StopIteration` from within the threadpool iterator
    # and catch it outside that context, so we coerce them into a different
    # exception type.
    try:
        return next(iterator)
    except StopIteration:
        raise _StopIteration


async def iterate_in_threadpool(
    iterator: Iterable[T],
) -> AsyncIterator[T]:
    as_iterator = iter(iterator)
    limiter = current_threadpool_limiter()
    while True:
        try:
            yield await anyio.to_thread.run_sync(_next, as_iterator, limiter=limiter)
        except _StopIteration:
            break
