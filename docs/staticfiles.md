
Starlette also includes a `StaticFiles` class for serving files in a given directory:

### StaticFiles

Signature: `StaticFiles(directory=None, packages=None, html=False, check_dir=True, follow_symlink=False, precompressed=False)`

* `directory` - A string or [os.PathLike][pathlike] denoting a directory path.
* `packages` - A list of strings or list of tuples of strings of python packages.
* `html` - Run in HTML mode. Automatically loads `index.html` for directories if such file exist.
* `check_dir` - Ensure that the directory exists upon instantiation. Defaults to `True`.
* `follow_symlink` - A boolean indicating if symbolic links for files and directories should be followed. Defaults to `False`.
* `precompressed` - A boolean indicating if precompressed `.br` and `.gz` variants of files should be served to clients that accept them. Defaults to `False`.

You can combine this ASGI application with Starlette's routing to provide
comprehensive static file serving.

```python
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles


routes = [
    ...
    Mount('/static', app=StaticFiles(directory='static'), name="static"),
]

app = Starlette(routes=routes)
```

Static files will respond with "404 Not found" or "405 Method not allowed"
responses for requests which do not match. In HTML mode if `404.html` file
exists it will be shown as 404 response.

The `packages` option can be used to include "static" directories contained within
a python package. The Python "bootstrap4" package is an example of this.

```python
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles


routes=[
    ...
    Mount('/static', app=StaticFiles(directory='static', packages=['bootstrap4']), name="static"),
]

app = Starlette(routes=routes)
```

By default `StaticFiles` will look for a `statics` directory in each package,
you can change the default directory by specifying a tuple of strings.

```python
routes=[
    ...
    Mount('/static', app=StaticFiles(packages=[('bootstrap4', 'static')]), name="static"),
]
```

You may prefer to include static files directly inside the "static" directory
rather than using Python packaging to include static files, but it can be useful
for bundling up reusable components.

### Precompressed files

Frontend build tooling often generates precompressed versions of static assets,
such as `app.js.br` and `app.js.gz` next to `app.js`. With `precompressed=True`,
`StaticFiles` negotiates one of these variants for clients that advertise
support for Brotli or gzip in their `Accept-Encoding` header, instead of
compressing responses on the fly.

```python
routes=[
    ...
    Mount('/static', app=StaticFiles(directory='static', precompressed=True), name="static"),
]
```

The variant is chosen by the quality weights of the `Accept-Encoding` header,
preferring Brotli over gzip on equal weights, and is only picked up when it
lives in the same directory as the original file. The precompressed file is
served as a representation of the original URL: the media type is derived from
the original file, a `Content-Encoding` header describes the variant, and each
representation gets its own `ETag`, so conditional requests and range requests
apply to the negotiated representation. Responses carry a
`Vary: Accept-Encoding` header so that caches store each representation
separately.

Clients that do not accept an available precompressed encoding receive the
original file, unless they explicitly refuse it with `identity;q=0`, in which
case a "406 Not Acceptable" response is returned.

[pathlike]: https://docs.python.org/3/library/os.html#os.PathLike
