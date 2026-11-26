import pytest

from starlette.applications import Starlette
from starlette.datastructures import URLPath
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import BaseRoute, Match, Mount, NoMatchFound, Route, Router
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send


async def endpoint(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@pytest.fixture
def reverse_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    original = Route.url_path_for

    def counted(self: Route, name: str, /, **path_params: object) -> URLPath:
        calls.append((self.name or "", name))
        return original(self, name, **path_params)

    monkeypatch.setattr(Route, "url_path_for", counted)
    return calls


def test_named_route_lookup_does_not_scan_unrelated_routes(
    reverse_calls: list[tuple[str, str]],
) -> None:
    for count in (40, 200, 1000):
        router = Router([Route(f"/items/{i}", endpoint, name=f"item-{i}") for i in range(count)])

        router.url_path_for("item-0")  # Build the index once.
        reverse_calls.clear()

        assert router.url_path_for(f"item-{count - 1}") == f"/items/{count - 1}"
        assert len(reverse_calls) == 1

        reverse_calls.clear()
        with pytest.raises(NoMatchFound):
            router.url_path_for("missing")
        assert reverse_calls == []


def test_same_name_routes_keep_order_and_parameter_selection() -> None:
    router = Router(
        [
            Route("/{value}", endpoint, name="dup"),
            Route("/{value}/{other}", endpoint, name="dup"),
            Route("/fixed", endpoint, name="dup"),
        ]
    )

    assert router.url_path_for("dup", value="one") == "/one"
    assert router.url_path_for("dup", value="one", other="two") == "/one/two"
    assert router.url_path_for("dup") == "/fixed"

    router.routes.reverse()
    assert router.url_path_for("dup") == "/fixed"


def test_route_list_mutations_are_immediately_visible() -> None:
    app = Starlette(routes=[Route(f"/old/{i}", endpoint, name=f"old-{i}") for i in range(3)])

    app.routes.insert(1, Route("/new/{value}", endpoint, name="new"))
    assert app.url_path_for("new", value="x") == "/new/x"

    app.routes[2] = Route("/replaced/{value}", endpoint, name="old-1")
    assert app.url_path_for("old-1", value="y") == "/replaced/y"

    del app.routes[1]
    with pytest.raises(NoMatchFound):
        app.url_path_for("new", value="x")

    order = [getattr(route, "name", None) for route in app.routes]
    app.routes.reverse()
    assert [getattr(route, "name", None) for route in app.routes] == list(reversed(order))

    app.routes.sort(key=lambda route: getattr(route, "path", ""))
    assert app.url_path_for("old-0") == "/old/0"

    app.routes[:] = [Route("/slice", endpoint, name="slice")]
    assert app.url_path_for("slice") == "/slice"

    app.routes = [Route("/reassigned", endpoint, name="reassigned")]
    assert app.url_path_for("reassigned") == "/reassigned"

    app.router.routes = [Route("/router-reassigned", endpoint, name="router-reassigned")]
    assert app.url_path_for("router-reassigned") == "/router-reassigned"

    in_place = Route("/in-place", endpoint, name="in-place")
    app.router.routes.append(in_place)
    in_place.name = "renamed-in-place"
    assert app.url_path_for("renamed-in-place") == "/in-place"


def test_nested_route_mutations_are_immediately_visible() -> None:
    child = Router([Route("/a/{value}", endpoint, name="a")])
    root = Router([Mount("/api", child, name="api")])

    child.routes.append(Route("/b/{value}", endpoint, name="b"))
    assert root.url_path_for("api:b", value="x") == "/api/b/x"

    mount = root.routes[0]
    assert isinstance(mount, Mount)
    mount.routes.insert(0, Route("/c/{value}", endpoint, name="c"))
    assert root.url_path_for("api:c", value="y") == "/api/c/y"

    del mount.routes[0]
    with pytest.raises(NoMatchFound):
        root.url_path_for("api:c", value="y")


class AliasRoute(BaseRoute):
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        return Match.NONE, {}

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        return None

    def url_path_for(self, name: str, /, **path_params: object) -> URLPath:
        if name == "custom-alias":
            return URLPath("/custom")
        raise NoMatchFound(name, path_params)


def test_custom_route_can_match_other_names_and_keeps_order() -> None:
    ordinary = Route("/ordinary", endpoint, name="ordinary")
    app = Router([ordinary, AliasRoute()])

    assert app.url_path_for("custom-alias") == "/custom"
    assert app.url_path_for("ordinary") == "/ordinary"

    class WinningAlias(AliasRoute):
        def url_path_for(self, name: str, /, **path_params: object) -> URLPath:
            if name == "ordinary":
                return URLPath("/custom-wins")
            return super().url_path_for(name, **path_params)

    ordered = Router([WinningAlias(), ordinary])
    assert ordered.url_path_for("ordinary") == "/custom-wins"


def test_application_and_request_url_generation_and_inbound_routing() -> None:
    app = Starlette(routes=[Route("/things/{item_id}", endpoint, name="thing")])
    client = TestClient(app)

    response = client.get("/things/7")
    assert response.status_code == 200
    assert app.url_path_for("thing", item_id=7) == "/things/7"

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/things/7",
        "raw_path": b"/things/7",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"root-path", b"/root")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "/root",
        "app": app,
        "router": app.router,
    }
    request = Request(scope)
    assert str(request.url_for("thing", item_id=8)) == "http://testserver/root/things/8"
