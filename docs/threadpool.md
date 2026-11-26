# Thread Pool

Starlette uses a thread pool in several scenarios to avoid blocking the event loop:

- When you create a synchronous endpoint using `def` instead of `async def`
- When serving files with [`FileResponse`](responses.md#fileresponse)
- When handling file uploads with [`UploadFile`](requests.md#request-files)
- When running synchronous background tasks with [`BackgroundTask`](background.md)
- And some other scenarios that may not be documented...

Starlette will run your code in a thread pool to avoid blocking the event loop.
This applies for endpoint functions and background tasks you create, but also for internal Starlette code.

To be more precise, Starlette uses `anyio.to_thread.run_sync` to run the synchronous code.

## Concurrency Limitations

The default thread pool size is only 40 _tokens_. This means that only 40 threads can run at the same time.
This limit is shared with other libraries: for example FastAPI also uses `anyio` to run sync dependencies, which also uses up thread capacity.

If you need to run more threads, you can increase the number of _tokens_:

```py
import anyio.to_thread

limiter = anyio.to_thread.current_default_thread_limiter()
limiter.total_tokens = 100
```

The above code will increase the number of _tokens_ to 100.

Increasing the number of threads may have a performance and memory impact, so be careful when doing so.

## Per-Application Capacity

When several Starlette applications are mounted in the same ASGI process, they
all draw from the default thread limiter described above. A single application
that saturates the pool can therefore delay the synchronous work of every
other application.

You can give an application its own thread pool quota with the
`threadpool_capacity` option. Synchronous routes, responses streamed from
synchronous iterators and synchronous background tasks then run against a
dedicated capacity limiter instead of the shared default one:

```py
from starlette.applications import Starlette

app = Starlette(routes=routes, threadpool_capacity=8)
```

With this option, at most 8 synchronous operations of `app` run at the same
time, regardless of how busy other applications are. When the option is not
set, the application keeps using AnyIO's default thread limiter, which is the
historical behavior.

A mounted application without `threadpool_capacity` inherits the limiter of
the application that mounts it, so its synchronous work shares the parent's
quota. A mounted application with its own `threadpool_capacity` uses its own
quota instead.

Asynchronous endpoints and other `async def` code keep running on the event
loop as before and do not consume any thread pool capacity.
