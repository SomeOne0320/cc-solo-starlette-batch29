import gzip
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio
import pytest

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles, identity_is_acceptable, parse_accept_encoding
from starlette.types import ASGIApp, Message, Scope
from starlette.websockets import WebSocketDisconnect
from tests.types import TestClientFactory

# Brotli-compressed representation of `b"<file content>"`. The `brotli` package
# is not a test dependency, so the bytes are hardcoded here.
BROTLI_CONTENT = bytes.fromhex("8b06803c66696c6520636f6e74656e743e03")


async def request_raw(app: ASGIApp, path: str, headers: dict[str, str] | None = None) -> tuple[int, Headers, bytes]:
    """
    Perform a request against an ASGI app without any client-side decoding, so
    tests can inspect the exact representation returned by the application.
    """
    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in (headers or {}).items()
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    status: int | None = None
    response_headers: Headers | None = None
    body = b""

    async def receive() -> Message:  # type: ignore[empty-body]
        ...  # pragma: no cover - StaticFiles does not read the request body.

    async def send(message: Message) -> None:
        nonlocal status, response_headers, body
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers = Headers(raw=message["headers"])
        else:
            assert message["type"] == "http.response.body"
            body += message["body"]

    await app(scope, receive, send)
    assert status is not None and response_headers is not None
    return status, response_headers, body


@pytest.fixture
def precompressed_app(tmp_path: Path) -> StaticFiles:
    # The app is built in a fixture so that the directory check in
    # `StaticFiles.__init__` does not run inside the event loop.
    content = b"<file content>"
    (tmp_path / "example.txt").write_bytes(content)
    (tmp_path / "example.txt.gz").write_bytes(gzip.compress(content, mtime=0))
    (tmp_path / "example.txt.br").write_bytes(BROTLI_CONTENT)
    return StaticFiles(directory=tmp_path, precompressed=True)


def test_staticfiles(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "<file content>"


def test_staticfiles_websocket(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    app = Starlette(routes=[Mount("/static", app=StaticFiles(directory=tmp_path))])
    client = test_client_factory(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/static/example.txt"):
            pass  # pragma: no cover - The connection is rejected before entering the context.

    assert exc.value.code == 1000


def test_staticfiles_with_pathlib(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    path = tmp_path / "example.txt"
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "<file content>"


def test_staticfiles_head_with_middleware(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    """
    see https://github.com/Kludex/starlette/pull/935
    """
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("x" * 100)

    async def does_nothing_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        return response

    routes = [Mount("/static", app=StaticFiles(directory=tmpdir), name="static")]
    middleware = [Middleware(BaseHTTPMiddleware, dispatch=does_nothing_middleware)]
    app = Starlette(routes=routes, middleware=middleware)

    client = test_client_factory(app)
    response = client.head("/static/example.txt")
    assert response.status_code == 200
    assert response.headers.get("content-length") == "100"


def test_staticfiles_with_package(test_client_factory: TestClientFactory) -> None:
    app = StaticFiles(packages=["tests"])
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "123\n"

    app = StaticFiles(packages=[("tests", "statics")])
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "123\n"


def test_staticfiles_post(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.post("/example.txt")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert response.text == "Method Not Allowed"


def test_staticfiles_with_directory_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_with_missing_file_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/404.txt")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_instantiated_with_missing_directory(tmpdir: Path) -> None:
    with pytest.raises(RuntimeError) as exc_info:
        path = os.path.join(tmpdir, "no_such_directory")
        StaticFiles(directory=path)
    assert "does not exist" in str(exc_info.value)


def test_staticfiles_configured_with_missing_directory(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "no_such_directory")
    app = StaticFiles(directory=path, check_dir=False)
    client = test_client_factory(app)
    with pytest.raises(RuntimeError) as exc_info:
        client.get("/example.txt")
    assert "does not exist" in str(exc_info.value)


def test_staticfiles_configured_with_file_instead_of_directory(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=path, check_dir=False)
    client = test_client_factory(app)
    with pytest.raises(RuntimeError) as exc_info:
        client.get("/example.txt")
    assert "is not a directory" in str(exc_info.value)


def test_staticfiles_config_check_occurs_only_once(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    assert not app.config_checked

    with pytest.raises(HTTPException):
        client.get("/")

    assert app.config_checked

    with pytest.raises(HTTPException):
        client.get("/")


def test_staticfiles_prevents_breaking_out_of_directory(tmpdir: Path) -> None:
    directory = os.path.join(tmpdir, "foo")
    os.mkdir(directory)

    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("outside root dir")

    app = StaticFiles(directory=directory)
    # We can't test this with 'httpx', so we test the app directly here.
    path = app.get_path({"path": "/../example.txt"})
    scope = {"method": "GET"}

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, scope)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_never_read_file_for_head_method(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    response = client.head("/example.txt")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "14"


def test_staticfiles_304_with_etag_match(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    first_resp = client.get("/example.txt")
    assert first_resp.status_code == 200
    last_etag = first_resp.headers["etag"]
    second_resp = client.get("/example.txt", headers={"if-none-match": last_etag})
    assert second_resp.status_code == 304
    assert second_resp.content == b""
    second_resp = client.get("/example.txt", headers={"if-none-match": f'W/{last_etag}, "123"'})
    assert second_resp.status_code == 304
    assert second_resp.content == b""
    second_resp = client.get("/example.txt", headers={"if-none-match": f'"123",\tW/{last_etag}'})
    assert second_resp.status_code == 304
    assert second_resp.content == b""


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("if_none_match", ["*", " \t* \t"])
def test_staticfiles_304_with_if_none_match_wildcard(
    tmp_path: Path,
    test_client_factory: TestClientFactory,
    method: str,
    if_none_match: str,
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)
    response = client.request(method, "/example.txt", headers={"if-none-match": if_none_match})
    assert response.status_code == 304
    assert response.content == b""


@pytest.mark.parametrize("if_none_match", ['"123"', '"*"', '"foo,*,bar"'])
def test_staticfiles_200_with_etag_mismatch(
    tmp_path: Path,
    test_client_factory: TestClientFactory,
    if_none_match: str,
) -> None:
    (tmp_path / "example.txt").write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"if-none-match": if_none_match})
    assert response.status_code == 200
    assert response.content == b"<file content>"


def test_staticfiles_200_with_etag_mismatch_and_timestamp_match(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path = tmpdir / "example.txt"
    path.write_text("<file content>", encoding="utf-8")

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    first_resp = client.get("/example.txt")
    assert first_resp.status_code == 200
    assert first_resp.headers["etag"] != '"123"'
    last_modified = first_resp.headers["last-modified"]
    # If `if-none-match` is present, `if-modified-since` is ignored.
    second_resp = client.get("/example.txt", headers={"if-none-match": '"123"', "if-modified-since": last_modified})
    assert second_resp.status_code == 200
    assert second_resp.content == b"<file content>"


def test_staticfiles_304_with_last_modified_compare_last_req(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path = os.path.join(tmpdir, "example.txt")
    file_last_modified_time = time.mktime(time.strptime("2013-10-10 23:40:00", "%Y-%m-%d %H:%M:%S"))
    with open(path, "w") as file:
        file.write("<file content>")
    os.utime(path, (file_last_modified_time, file_last_modified_time))

    app = StaticFiles(directory=tmpdir)
    client = test_client_factory(app)
    # last modified less than last request, 304
    response = client.get("/example.txt", headers={"If-Modified-Since": "Thu, 11 Oct 2013 15:30:19 GMT"})
    assert response.status_code == 304
    assert response.content == b""
    # last modified greater than last request, 200 with content
    response = client.get("/example.txt", headers={"If-Modified-Since": "Thu, 20 Feb 2012 15:30:19 GMT"})
    assert response.status_code == 200
    assert response.content == b"<file content>"


def test_staticfiles_html_normal(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "404.html")
    with open(path, "w") as file:
        file.write("<h1>Custom not found page</h1>")
    path = os.path.join(tmpdir, "dir")
    os.mkdir(path)
    path = os.path.join(path, "index.html")
    with open(path, "w") as file:
        file.write("<h1>Hello</h1>")

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    response = client.get("/dir/")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/dir")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/dir/index.html")
    assert response.url == "http://testserver/dir/index.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/missing")
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"


def test_staticfiles_html_without_index(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "404.html")
    with open(path, "w") as file:
        file.write("<h1>Custom not found page</h1>")
    path = os.path.join(tmpdir, "dir")
    os.mkdir(path)

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    response = client.get("/dir/")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"

    response = client.get("/dir")
    assert response.url == "http://testserver/dir"
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"

    response = client.get("/missing")
    assert response.status_code == 404
    assert response.text == "<h1>Custom not found page</h1>"


def test_staticfiles_html_without_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "dir")
    os.mkdir(path)
    path = os.path.join(path, "index.html")
    with open(path, "w") as file:
        file.write("<h1>Hello</h1>")

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    response = client.get("/dir/")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    response = client.get("/dir")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"

    with pytest.raises(HTTPException) as exc_info:
        response = client.get("/missing")
    assert exc_info.value.status_code == 404


def test_staticfiles_html_only_files(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "hello.html")
    with open(path, "w") as file:
        file.write("<h1>Hello</h1>")

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    with pytest.raises(HTTPException) as exc_info:
        response = client.get("/")
    assert exc_info.value.status_code == 404

    response = client.get("/hello.html")
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_cache_invalidation_for_deleted_file_html_mode(
    tmpdir: Path, test_client_factory: TestClientFactory
) -> None:
    path_404 = os.path.join(tmpdir, "404.html")
    with open(path_404, "w") as file:
        file.write("<p>404 file</p>")
    path_some = os.path.join(tmpdir, "some.html")
    with open(path_some, "w") as file:
        file.write("<p>some file</p>")

    common_modified_time = time.mktime(time.strptime("2013-10-10 23:40:00", "%Y-%m-%d %H:%M:%S"))
    os.utime(path_404, (common_modified_time, common_modified_time))
    os.utime(path_some, (common_modified_time, common_modified_time))

    app = StaticFiles(directory=tmpdir, html=True)
    client = test_client_factory(app)

    resp_exists = client.get("/some.html")
    assert resp_exists.status_code == 200
    assert resp_exists.text == "<p>some file</p>"

    resp_cached = client.get(
        "/some.html",
        headers={"If-Modified-Since": resp_exists.headers["last-modified"]},
    )
    assert resp_cached.status_code == 304

    os.remove(path_some)

    resp_deleted = client.get(
        "/some.html",
        headers={"If-Modified-Since": resp_exists.headers["last-modified"]},
    )
    assert resp_deleted.status_code == 404
    assert resp_deleted.text == "<p>404 file</p>"


def test_staticfiles_with_invalid_dir_permissions_returns_401(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_bytes(b"<file content>")

    original_mode = tmp_path.stat().st_mode
    tmp_path.chmod(stat.S_IRWXO)
    try:
        routes = [
            Mount(
                "/",
                app=StaticFiles(directory=os.fsdecode(tmp_path)),
                name="static",
            )
        ]
        app = Starlette(routes=routes)
        client = test_client_factory(app)

        response = client.get("/example.txt")
        assert response.status_code == 401
        assert response.text == "Unauthorized"
    finally:
        tmp_path.chmod(original_mode)


def test_staticfiles_with_missing_dir_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/foo/example.txt")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_access_file_as_dir_returns_404(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/example.txt/foo")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_null_byte_in_path(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    response = client.get("/example%00.txt")
    assert response.status_code == 404


@pytest.mark.skipif(not hasattr(os, "pathconf"), reason="os.pathconf is Unix-only")
def test_staticfiles_filename_too_long(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app)

    path_max_size = os.pathconf("/", "PC_PATH_MAX")
    response = client.get(f"/{'a' * path_max_size}.txt")
    assert response.status_code == 404
    assert response.text == "Not Found"


def test_staticfiles_unhandled_os_error_returns_500(
    tmpdir: Path,
    test_client_factory: TestClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_timeout(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError

    path = os.path.join(tmpdir, "example.txt")
    with open(path, "w") as file:
        file.write("<file content>")

    routes = [Mount("/", app=StaticFiles(directory=tmpdir), name="static")]
    app = Starlette(routes=routes)
    client = test_client_factory(app, raise_server_exceptions=False)

    monkeypatch.setattr("starlette.staticfiles.StaticFiles.lookup_path", mock_timeout)

    response = client.get("/example.txt")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


def test_staticfiles_follows_symlinks(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    statics_path = os.path.join(tmpdir, "statics")
    os.mkdir(statics_path)

    source_path = tempfile.mkdtemp()
    source_file_path = os.path.join(source_path, "page.html")
    with open(source_file_path, "w") as file:
        file.write("<h1>Hello</h1>")

    statics_file_path = os.path.join(statics_path, "index.html")
    os.symlink(source_file_path, statics_file_path)

    app = StaticFiles(directory=statics_path, follow_symlink=True)
    client = test_client_factory(app)

    response = client.get("/index.html")
    assert response.url == "http://testserver/index.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_follows_symlink_directories(tmpdir: Path, test_client_factory: TestClientFactory) -> None:
    statics_path = os.path.join(tmpdir, "statics")
    statics_html_path = os.path.join(statics_path, "html")
    os.mkdir(statics_path)

    source_path = tempfile.mkdtemp()
    source_file_path = os.path.join(source_path, "page.html")
    with open(source_file_path, "w") as file:
        file.write("<h1>Hello</h1>")

    os.symlink(source_path, statics_html_path)

    app = StaticFiles(directory=statics_path, follow_symlink=True)
    client = test_client_factory(app)

    response = client.get("/html/page.html")
    assert response.url == "http://testserver/html/page.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_disallows_path_traversal_with_symlinks(tmpdir: Path) -> None:
    statics_path = os.path.join(tmpdir, "statics")

    root_source_path = tempfile.mkdtemp()
    source_path = os.path.join(root_source_path, "statics")
    os.mkdir(source_path)

    source_file_path = os.path.join(root_source_path, "index.html")
    with open(source_file_path, "w") as file:
        file.write("<h1>Hello</h1>")

    os.symlink(source_path, statics_path)

    app = StaticFiles(directory=statics_path, follow_symlink=True)
    # We can't test this with 'httpx', so we test the app directly here.
    path = app.get_path({"path": "/../index.html"})
    scope = {"method": "GET"}

    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, scope)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_avoids_path_traversal(tmp_path: Path) -> None:
    statics_path = tmp_path / "static"
    statics_disallow_path = tmp_path / "static_disallow"

    statics_path.mkdir()
    statics_disallow_path.mkdir()

    static_index_file = statics_path / "index.html"
    statics_disallow_path_index_file = statics_disallow_path / "index.html"
    static_file = tmp_path / "static1.txt"

    static_index_file.write_text("<h1>Hello</h1>")
    statics_disallow_path_index_file.write_text("<h1>Private</h1>")
    static_file.write_text("Private")

    app = StaticFiles(directory=statics_path)

    # We can't test this with 'httpx', so we test the app directly here.
    path = app.get_path({"path": "/../static1.txt"})
    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, {"method": "GET"})

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"

    path = app.get_path({"path": "/../static_disallow/index.html"})
    with pytest.raises(HTTPException) as exc_info:
        anyio.run(app.get_response, path, {"method": "GET"})

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not Found"


def test_staticfiles_rejects_absolute_paths(tmp_path: Path) -> None:
    statics_path = tmp_path / "static"
    statics_path.mkdir()
    app = StaticFiles(directory=statics_path)

    full_path, stat_result = app.lookup_path("/etc/passwd")
    assert full_path == ""
    assert stat_result is None


def test_staticfiles_rejects_absolute_windows_paths(tmp_path: Path) -> None:
    statics_path = tmp_path / "static"
    statics_path.mkdir()
    app = StaticFiles(directory=statics_path)

    full_path, stat_result = app.lookup_path("\\\\server\\share")
    assert full_path == ""
    assert stat_result is None


def test_staticfiles_self_symlinks(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    statics_path = tmp_path / "statics"
    statics_path.mkdir()

    source_file_path = statics_path / "index.html"
    source_file_path.write_text("<h1>Hello</h1>", encoding="utf-8")

    statics_symlink_path = tmp_path / "statics_symlink"
    statics_symlink_path.symlink_to(statics_path)

    app = StaticFiles(directory=statics_symlink_path, follow_symlink=True)
    client = test_client_factory(app)

    response = client.get("/index.html")
    assert response.url == "http://testserver/index.html"
    assert response.status_code == 200
    assert response.text == "<h1>Hello</h1>"


def test_staticfiles_relative_directory_symlinks(test_client_factory: TestClientFactory) -> None:
    app = StaticFiles(directory="tests/statics", follow_symlink=True)
    client = test_client_factory(app)
    response = client.get("/example.txt")
    assert response.status_code == 200
    assert response.text == "123\n"


def test_staticfiles_precompressed_disabled_by_default(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    content = b"<file content>"
    (tmp_path / "example.txt").write_bytes(content)
    (tmp_path / "example.txt.gz").write_bytes(gzip.compress(content, mtime=0))

    app = StaticFiles(directory=tmp_path)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.content == content
    assert "content-encoding" not in response.headers
    assert "vary" not in response.headers


def test_staticfiles_precompressed_gzip(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    content = b"<file content>"
    gz_content = gzip.compress(content, mtime=0)
    (tmp_path / "example.txt").write_bytes(content)
    (tmp_path / "example.txt.gz").write_bytes(gz_content)

    app = StaticFiles(directory=tmp_path, precompressed=True)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["content-length"] == str(len(gz_content))
    # The media type is derived from the original file, not the variant.
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["etag"].endswith('-gzip"')
    # The client transparently decodes the precompressed representation.
    assert response.content == content


@pytest.mark.anyio
async def test_staticfiles_precompressed_brotli(precompressed_app: StaticFiles) -> None:
    status, headers, body = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "br"})
    assert status == 200
    assert headers["content-encoding"] == "br"
    assert headers["vary"] == "Accept-Encoding"
    assert headers["content-length"] == str(len(BROTLI_CONTENT))
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert headers["etag"].endswith('-br"')
    assert body == BROTLI_CONTENT


@pytest.mark.anyio
async def test_staticfiles_precompressed_content_negotiation(precompressed_app: StaticFiles) -> None:
    content = b"<file content>"
    gz_content = gzip.compress(content, mtime=0)

    # Brotli is preferred when the client weights both encodings equally.
    status, headers, body = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "gzip, br"})
    assert status == 200
    assert headers["content-encoding"] == "br"
    assert body == BROTLI_CONTENT

    # Quality weights are honored.
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "br;q=0.5, gzip;q=1.0"}
    )
    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert body == gz_content

    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "br;q=1.0, gzip;q=0.5"}
    )
    assert status == 200
    assert headers["content-encoding"] == "br"
    assert body == BROTLI_CONTENT

    # An encoding with a zero weight is not acceptable.
    status, headers, body = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "br;q=0, gzip"})
    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert body == gz_content

    # The original file is served when no variant is acceptable...
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "br;q=0, gzip;q=0"}
    )
    assert status == 200
    assert "content-encoding" not in headers
    assert headers["vary"] == "Accept-Encoding"
    assert body == content

    # ... and when the client does not send an "Accept-Encoding" header.
    status, headers, body = await request_raw(precompressed_app, "/example.txt")
    assert status == 200
    assert "content-encoding" not in headers
    assert body == content


@pytest.mark.anyio
async def test_staticfiles_precompressed_wildcard(precompressed_app: StaticFiles) -> None:
    status, headers, body = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "*"})
    assert status == 200
    assert headers["content-encoding"] == "br"
    assert body == BROTLI_CONTENT


def test_staticfiles_precompressed_falls_back_to_identity(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    (tmp_path / "example.txt").write_bytes(b"<file content>")

    app = StaticFiles(directory=tmp_path, precompressed=True)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"accept-encoding": "gzip, br"})
    assert response.status_code == 200
    assert response.content == b"<file content>"
    assert "content-encoding" not in response.headers
    assert response.headers["vary"] == "Accept-Encoding"


def test_staticfiles_precompressed_identity_refused(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    (tmp_path / "example.txt").write_bytes(b"<file content>")

    app = Starlette(routes=[Mount("/", app=StaticFiles(directory=tmp_path, precompressed=True))])
    client = test_client_factory(app)

    # No variant exists, and the client refuses the original file.
    response = client.get("/example.txt", headers={"accept-encoding": "gzip;q=0, identity;q=0"})
    assert response.status_code == 406
    assert response.headers["vary"] == "Accept-Encoding"

    response = client.get("/example.txt", headers={"accept-encoding": "*;q=0"})
    assert response.status_code == 406


def test_staticfiles_precompressed_identity_refused_but_variant_available(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    content = b"<file content>"
    (tmp_path / "example.txt").write_bytes(content)
    (tmp_path / "example.txt.gz").write_bytes(gzip.compress(content, mtime=0))

    app = Starlette(routes=[Mount("/", app=StaticFiles(directory=tmp_path, precompressed=True))])
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"accept-encoding": "gzip, identity;q=0"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == content


@pytest.mark.anyio
async def test_staticfiles_precompressed_etag_per_representation(precompressed_app: StaticFiles) -> None:
    _, identity_headers, _ = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "identity"})
    _, gzip_headers, _ = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "gzip"})
    _, br_headers, _ = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "br"})
    etags = {identity_headers["etag"], gzip_headers["etag"], br_headers["etag"]}
    assert len(etags) == 3


@pytest.mark.anyio
async def test_staticfiles_precompressed_not_modified(precompressed_app: StaticFiles) -> None:
    gz_content = gzip.compress(b"<file content>", mtime=0)

    _, headers, _ = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "gzip"})
    gzip_etag = headers["etag"]
    gzip_last_modified = headers["last-modified"]

    # The client holds the representation that gets selected.
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "gzip", "if-none-match": gzip_etag}
    )
    assert status == 304
    assert body == b""
    assert headers["etag"] == gzip_etag
    assert headers["vary"] == "Accept-Encoding"

    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "gzip", "if-modified-since": gzip_last_modified}
    )
    assert status == 304
    assert body == b""

    # The client holds a different representation than the selected one.
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "br", "if-none-match": gzip_etag}
    )
    assert status == 200
    assert headers["content-encoding"] == "br"
    assert body == BROTLI_CONTENT

    _, headers, _ = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "identity"})
    identity_etag = headers["etag"]
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "gzip", "if-none-match": identity_etag}
    )
    assert status == 200
    assert headers["content-encoding"] == "gzip"
    assert body == gz_content


@pytest.mark.anyio
async def test_staticfiles_precompressed_range(precompressed_app: StaticFiles) -> None:
    gz_content = gzip.compress(b"<file content>", mtime=0)

    _, headers, _ = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "gzip"})
    gzip_etag = headers["etag"]

    # Ranges apply to the selected (compressed) representation.
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "gzip", "range": "bytes=2-5"}
    )
    assert status == 206
    assert headers["content-encoding"] == "gzip"
    assert headers["content-range"] == f"bytes 2-5/{len(gz_content)}"
    assert body == gz_content[2:6]

    # A range request is only resumed when the client holds the selected representation.
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "gzip", "range": "bytes=2-5", "if-range": gzip_etag}
    )
    assert status == 206
    assert body == gz_content[2:6]

    _, headers, _ = await request_raw(precompressed_app, "/example.txt", {"accept-encoding": "identity"})
    identity_etag = headers["etag"]
    status, headers, body = await request_raw(
        precompressed_app, "/example.txt", {"accept-encoding": "gzip", "range": "bytes=2-5", "if-range": identity_etag}
    )
    assert status == 200
    assert body == gz_content


def test_staticfiles_precompressed_head(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    content = b"<file content>"
    gz_content = gzip.compress(content, mtime=0)
    (tmp_path / "example.txt").write_bytes(content)
    (tmp_path / "example.txt.gz").write_bytes(gz_content)

    app = StaticFiles(directory=tmp_path, precompressed=True)
    client = test_client_factory(app)
    response = client.head("/example.txt", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-length"] == str(len(gz_content))
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.content == b""


def test_staticfiles_precompressed_does_not_combine_directories(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "example.txt").write_bytes(b"first")
    (second / "example.txt.gz").write_bytes(gzip.compress(b"second", mtime=0))

    app = StaticFiles(directory=first, precompressed=True)
    app.all_directories = [*app.all_directories, second]

    client = test_client_factory(app)
    # The original file is found in the first directory. The variant living in
    # the second directory must not be combined with it.
    response = client.get("/example.txt", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.content == b"first"
    assert "content-encoding" not in response.headers


def test_staticfiles_precompressed_variant_must_live_next_to_original(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "example.txt.gz").write_bytes(gzip.compress(b"first", mtime=0))
    (second / "example.txt").write_bytes(b"second")

    app = StaticFiles(directory=first, precompressed=True)
    app.all_directories = [*app.all_directories, second]

    client = test_client_factory(app)
    # The original file is found in the second directory, which has no variant.
    response = client.get("/example.txt", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.content == b"second"
    assert "content-encoding" not in response.headers


def test_staticfiles_precompressed_html_index(tmp_path: Path, test_client_factory: TestClientFactory) -> None:
    content = b"<h1>Hello</h1>"
    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "index.html").write_bytes(content)
    (directory / "index.html.gz").write_bytes(gzip.compress(content, mtime=0))

    app = StaticFiles(directory=tmp_path, html=True, precompressed=True)
    client = test_client_factory(app)

    response = client.get("/dir/")
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.content == content

    response = client.get("/dir")
    assert response.url == "http://testserver/dir/"
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == content


def test_staticfiles_precompressed_ignores_non_file_variant(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    content = b"<file content>"
    (tmp_path / "example.txt").write_bytes(content)
    (tmp_path / "example.txt.gz").mkdir()
    (tmp_path / "example.txt.br").mkdir()

    app = StaticFiles(directory=tmp_path, precompressed=True)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"accept-encoding": "gzip, br"})
    assert response.status_code == 200
    assert response.content == content
    assert "content-encoding" not in response.headers


def test_staticfiles_precompressed_rejects_escaping_symlinked_variant(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    statics = tmp_path / "statics"
    statics.mkdir()
    (statics / "example.txt").write_bytes(b"<file content>")
    outside = tmp_path / "outside.txt.gz"
    outside.write_bytes(gzip.compress(b"outside", mtime=0))
    (statics / "example.txt.gz").symlink_to(outside)

    app = StaticFiles(directory=statics, precompressed=True)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.content == b"<file content>"
    assert "content-encoding" not in response.headers


def test_staticfiles_precompressed_follows_symlinked_variant_when_allowed(
    tmp_path: Path, test_client_factory: TestClientFactory
) -> None:
    statics = tmp_path / "statics"
    statics.mkdir()
    (statics / "example.txt").write_bytes(b"<file content>")
    outside = tmp_path / "outside.txt.gz"
    outside.write_bytes(gzip.compress(b"outside", mtime=0))
    (statics / "example.txt.gz").symlink_to(outside)

    app = StaticFiles(directory=statics, precompressed=True, follow_symlink=True)
    client = test_client_factory(app)
    response = client.get("/example.txt", headers={"accept-encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == b"outside"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", {}),
        ("gzip, br", {"gzip": 1.0, "br": 1.0}),
        ("gzip;q=0.8, br;q=0", {"gzip": 0.8, "br": 0.0}),
        ("GZip ; Q=0.5", {"gzip": 0.5}),
        ("gzip;q=abc", {"gzip": 1.0}),
        ("gzip;q=2.0", {"gzip": 1.0}),
        ("gzip;level=1", {"gzip": 1.0}),
        ("identity;q=0, *;q=0.5", {"identity": 0.0, "*": 0.5}),
    ],
)
def test_parse_accept_encoding(header: str, expected: dict[str, float]) -> None:
    assert parse_accept_encoding(header) == expected


@pytest.mark.parametrize(
    ("encodings", "expected"),
    [
        ({}, True),
        ({"gzip": 1.0}, True),
        ({"identity": 0.0}, False),
        ({"identity": 0.5}, True),
        ({"*": 0.0}, False),
        ({"*": 0.0, "identity": 0.5}, True),
    ],
)
def test_identity_is_acceptable(encodings: dict[str, float], expected: bool) -> None:
    assert identity_is_acceptable(encodings) is expected
