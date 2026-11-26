from __future__ import annotations

import errno
import importlib.util
import os
import stat
from email.utils import parsedate
from mimetypes import guess_type
from typing import Union

import anyio
import anyio.to_thread

from starlette._utils import get_route_path
from starlette.datastructures import URL, Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketClose

PathLike = Union[str, "os.PathLike[str]"]

# Precompressed variants supported by `StaticFiles(precompressed=True)`, as
# `(content encoding, file extension)` pairs, in decreasing order of
# preference for clients that weight them equally.
PRECOMPRESSED_VARIANTS = (("br", ".br"), ("gzip", ".gz"))


def parse_accept_encoding(accept_encoding: str) -> dict[str, float]:
    """
    Parse an "Accept-Encoding" header into a mapping of content coding to
    quality weight. Entries with an invalid quality weight are ignored.
    """
    encodings: dict[str, float] = {}
    for item in accept_encoding.split(","):
        coding, _, parameters = item.partition(";")
        coding = coding.strip().lower()
        if not coding:
            continue
        q_value = 1.0
        for parameter in parameters.split(";"):
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                try:
                    q_value = min(max(float(value), 0.0), 1.0)
                except ValueError:
                    pass  # Keep the default quality weight.
                break
        encodings[coding] = q_value
    return encodings


def identity_is_acceptable(encodings: dict[str, float]) -> bool:
    """
    Given the quality weights parsed from an "Accept-Encoding" header, return
    `True` if the unencoded ("identity") representation is acceptable. It is
    acceptable by default, unless refused with "identity;q=0", or with "*;q=0"
    and no more specific "identity" entry.
    """
    if "identity" in encodings:
        return encodings["identity"] > 0
    return encodings.get("*", 1.0) > 0


class NotModifiedResponse(Response):
    NOT_MODIFIED_HEADERS = (
        "cache-control",
        "content-location",
        "date",
        "etag",
        "expires",
        "vary",
    )

    def __init__(self, headers: Headers):
        super().__init__(
            status_code=304,
            headers={name: value for name, value in headers.items() if name in self.NOT_MODIFIED_HEADERS},
        )


class StaticFiles:
    def __init__(
        self,
        *,
        directory: PathLike | None = None,
        packages: list[str | tuple[str, str]] | None = None,
        html: bool = False,
        check_dir: bool = True,
        follow_symlink: bool = False,
        precompressed: bool = False,
    ) -> None:
        self.directory = directory
        self.packages = packages
        self.all_directories = self.get_directories(directory, packages)
        self.html = html
        self.config_checked = False
        self.follow_symlink = follow_symlink
        self.precompressed = precompressed
        if check_dir and directory is not None and not os.path.isdir(directory):
            raise RuntimeError(f"Directory '{directory}' does not exist")

    def get_directories(
        self,
        directory: PathLike | None = None,
        packages: list[str | tuple[str, str]] | None = None,
    ) -> list[PathLike]:
        """
        Given `directory` and `packages` arguments, return a list of all the
        directories that should be used for serving static files from.
        """
        directories = []
        if directory is not None:
            directories.append(directory)

        for package in packages or []:
            if isinstance(package, tuple):
                package, statics_dir = package
            else:
                statics_dir = "statics"
            spec = importlib.util.find_spec(package)
            assert spec is not None, f"Package {package!r} could not be found."
            assert spec.origin is not None, f"Package {package!r} could not be found."
            package_directory = os.path.normpath(os.path.join(spec.origin, "..", statics_dir))
            assert os.path.isdir(package_directory), (
                f"Directory '{statics_dir!r}' in package {package!r} could not be found."
            )
            directories.append(package_directory)

        return directories

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        The ASGI entry point.
        """
        if scope["type"] == "websocket":
            websocket_close = WebSocketClose()
            await websocket_close(scope, receive, send)
            return

        assert scope["type"] == "http"

        if not self.config_checked:
            await self.check_config()
            self.config_checked = True

        path = self.get_path(scope)
        response = await self.get_response(path, scope)
        await response(scope, receive, send)

    def get_path(self, scope: Scope) -> str:
        """
        Given the ASGI scope, return the `path` string to serve up,
        with OS specific path separators, and any '..', '.' components removed.
        """
        route_path = get_route_path(scope)
        return os.path.normpath(os.path.join(*route_path.split("/")))

    async def get_response(self, path: str, scope: Scope) -> Response:
        """
        Returns an HTTP response, given the incoming path, method and request headers.
        """
        if scope["method"] not in ("GET", "HEAD"):
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})

        try:
            full_path, stat_result = await anyio.to_thread.run_sync(self.lookup_path, path)
        except PermissionError:
            raise HTTPException(status_code=401)
        except OSError as exc:
            # Filename is too long, so it can't be a valid static file.
            if exc.errno == errno.ENAMETOOLONG:
                raise HTTPException(status_code=404)

            raise exc
        except ValueError:
            # Null bytes or other invalid characters in the path.
            raise HTTPException(status_code=404)

        if stat_result and stat.S_ISREG(stat_result.st_mode):
            # We have a static file to serve.
            if self.precompressed:
                return await self.precompressed_file_response(full_path, stat_result, scope)
            return self.file_response(full_path, stat_result, scope)

        elif stat_result and stat.S_ISDIR(stat_result.st_mode) and self.html:
            # We're in HTML mode, and have got a directory URL.
            # Check if we have 'index.html' file to serve.
            index_path = os.path.join(path, "index.html")
            full_path, stat_result = await anyio.to_thread.run_sync(self.lookup_path, index_path)
            if stat_result is not None and stat.S_ISREG(stat_result.st_mode):
                if not scope["path"].endswith("/"):
                    # Directory URLs should redirect to always end in "/".
                    url = URL(scope=scope)
                    url = url.replace(path=url.path + "/")
                    return RedirectResponse(url=url)
                if self.precompressed:
                    return await self.precompressed_file_response(full_path, stat_result, scope)
                return self.file_response(full_path, stat_result, scope)

        if self.html:
            # Check for '404.html' if we're in HTML mode.
            full_path, stat_result = await anyio.to_thread.run_sync(self.lookup_path, "404.html")
            if stat_result and stat.S_ISREG(stat_result.st_mode):
                return FileResponse(full_path, stat_result=stat_result, status_code=404)
        raise HTTPException(status_code=404)

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        # Reject absolute paths so they cannot escape the served directory.
        if path.startswith(("/", "\\")):
            return "", None
        for directory in self.all_directories:
            joined_path = os.path.join(directory, path)
            if self.follow_symlink:
                full_path = os.path.abspath(joined_path)
                directory = os.path.abspath(directory)
            else:
                full_path = os.path.realpath(joined_path)
                directory = os.path.realpath(directory)
            if os.path.commonpath([full_path, directory]) != str(directory):
                # Don't allow misbehaving clients to break out of the static files directory.
                continue
            try:
                return full_path, os.stat(full_path)
            except (FileNotFoundError, NotADirectoryError):
                continue
        return "", None

    def lookup_precompressed_variant(
        self, full_path: str, encodings: dict[str, float]
    ) -> tuple[str, str, os.stat_result] | None:
        """
        Given the path of a static file and the quality weights parsed from the
        "Accept-Encoding" header, return the `(encoding, path, stat_result)` of
        the most preferred precompressed variant that exists next to it, or
        `None` if the client accepts none of the available variants.
        """
        wildcard_q_value = encodings.get("*", 0.0)
        best: tuple[float, str, str, os.stat_result] | None = None
        # Variants are listed in decreasing order of preference, so the first
        # one seen wins when quality weights are equal.
        for encoding, extension in PRECOMPRESSED_VARIANTS:
            q_value = encodings.get(encoding, wildcard_q_value)
            if q_value <= 0:
                # The client does not accept this encoding.
                continue
            variant_path = full_path + extension
            try:
                variant_stat = os.stat(variant_path)
            except OSError:
                continue
            if not stat.S_ISREG(variant_stat.st_mode):
                continue
            if not self.follow_symlink:
                # Don't allow a symlinked variant to break out of the directory
                # containing the original file.
                directory = os.path.dirname(full_path)
                if os.path.commonpath([os.path.realpath(variant_path), directory]) != directory:
                    continue
            if best is None or q_value > best[0]:
                best = (q_value, encoding, variant_path, variant_stat)
        if best is None:
            return None
        return best[1], best[2], best[3]

    async def precompressed_file_response(
        self,
        full_path: str,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """
        Serve a static file, negotiating a precompressed variant ("<file>.br"
        or "<file>.gz") when the client advertises support for the encoding in
        its "Accept-Encoding" header.
        """
        encoding: str | None = None
        media_type: str | None = None
        request_headers = Headers(scope=scope)
        accept_encoding = request_headers.get("accept-encoding")
        if accept_encoding is not None:
            encodings = parse_accept_encoding(accept_encoding)
            variant = await anyio.to_thread.run_sync(self.lookup_precompressed_variant, full_path, encodings)
            if variant is None:
                # There is no precompressed variant the client accepts. Fall
                # back to the original file, unless the client refuses it.
                if not identity_is_acceptable(encodings):
                    raise HTTPException(status_code=406, headers={"Vary": "Accept-Encoding"})
            else:
                encoding, variant_path, variant_stat = variant
                # The media type is derived from the original file. The
                # precompressed file is just another representation of it.
                media_type = guess_type(full_path)[0]
                full_path, stat_result = variant_path, variant_stat
        return self.file_response(full_path, stat_result, scope, status_code, encoding, media_type)

    def file_response(
        self,
        full_path: PathLike,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
        encoding: str | None = None,
        media_type: str | None = None,
    ) -> Response:
        request_headers = Headers(scope=scope)

        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result, media_type=media_type)
        if self.precompressed:
            # The selected representation depends on the "Accept-Encoding"
            # header, so caches need to store each of them separately.
            response.headers.add_vary_header("Accept-Encoding")
        if encoding is not None:
            response.headers["content-encoding"] = encoding
            # Each representation gets its own ETag, so that a "304 Not
            # Modified" is only returned when the client already holds the
            # representation that was selected for this request.
            etag = response.headers["etag"]
            response.headers["etag"] = f'{etag[:-1]}-{encoding}"'
        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response

    async def check_config(self) -> None:
        """
        Perform a one-off configuration check that StaticFiles is actually
        pointed at a directory, so that we can raise loud errors rather than
        just returning 404 responses.
        """
        if self.directory is None:
            return

        try:
            stat_result = await anyio.to_thread.run_sync(os.stat, self.directory)
        except FileNotFoundError:
            raise RuntimeError(f"StaticFiles directory '{self.directory}' does not exist.")
        if not (stat.S_ISDIR(stat_result.st_mode) or stat.S_ISLNK(stat_result.st_mode)):
            raise RuntimeError(f"StaticFiles path '{self.directory}' is not a directory.")

    def is_not_modified(self, response_headers: Headers, request_headers: Headers) -> bool:
        """
        Given the request and response headers, return `True` if an HTTP
        "Not Modified" response could be returned instead.
        """
        if if_none_match := request_headers.get("if-none-match"):
            if if_none_match.strip() == "*":
                return True
            # The "etag" header is added by FileResponse, so it's always present.
            etag = response_headers["etag"]
            return etag in [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]

        try:
            if_modified_since = parsedate(request_headers["if-modified-since"])
            last_modified = parsedate(response_headers["last-modified"])
            if if_modified_since is not None and last_modified is not None and if_modified_since >= last_modified:
                return True
        except KeyError:
            pass

        return False
