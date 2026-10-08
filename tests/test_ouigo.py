"""Regression tests for the read-only OUIGO Spain example."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from examples import ouigo_server

STATIONS = [
    {
        "_u_i_c_station_code": "7171801",
        "name": "Barcelona - Sants",
        "connected_stations": ["MT1", "7104104"],
        "synonyms": ["Barcelona"],
        "short_code": "BAR",
        "hidden": False,
    },
    {
        "_u_i_c_station_code": "7150500",
        "name": "Córdoba - Julio Anguita",
        "connected_stations": ["MT1"],
        "synonyms": ["Córdoba"],
        "short_code": "COR",
        "hidden": False,
    },
    {
        "_u_i_c_station_code": "MT1",
        "name": "Madrid - Todas las estaciones",
        "connected_stations": ["7171801", "7150500"],
        "synonyms": ["Madrid"],
        "short_code": "MT1",
        "hidden": False,
    },
    {
        "_u_i_c_station_code": "7160000",
        "name": "Madrid - Puerta de Atocha - Almudena Grandes",
        "connected_stations": ["7171801"],
        "synonyms": ["Madrid"],
        "short_code": "MAT",
        "hidden": True,
    },
    {
        "_u_i_c_station_code": "7154413",
        "name": "Málaga - María Zambrano",
        "connected_stations": ["MT1"],
        "synonyms": ["Málaga"],
        "short_code": "MAL",
        "hidden": False,
    },
]

JOURNEY = {
    "segments": [{"service_name": "06471", "service_date": "2026-10-14"}],
    "departure_station": {
        "departure_timestamp": "2026-10-14T06:22:00+02:00",
        "_u_i_c_station_code": "7160000",
        "name": "Madrid - Puerta de Atocha - Almudena Grandes",
    },
    "arrival_station": {
        "arrival_timestamp": "2026-10-14T09:45:00+02:00",
        "_u_i_c_station_code": "7171801",
        "name": "Barcelona - Sants",
    },
    "full": False,
    "remaining_seats": None,
    "price": 33.0,
    "original_price": 33.0,
    "is_promo": False,
    "packages": [
        {"product_family_id": "OUIGO_PLUS", "name": "Ouigo PLUS", "price": 42.0},
        {"product_family_id": "OUIGO_FULL", "name": "Ouigo FULL", "price": 50.0},
    ],
}


class FakeOuigo:
    """A local stand-in for the OUIGO web API that records each request."""

    def __init__(self, routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.logins = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Yield like real network I/O so concurrent tool calls interleave.
        await asyncio.sleep(0)
        path = request.url.path.removeprefix("/api")
        if path == "/Token/login":
            self.logins += 1
            return httpx.Response(200, json={"token": f"token-{self.logins}"})
        if path == "/Data/GetStations":
            return httpx.Response(200, json=STATIONS)
        if path in self.routes:
            return self.routes[path](request)
        raise AssertionError(f"Unexpected OUIGO request: {request.method} {request.url}")

    def bodies(self, path: str) -> list[Any]:
        return [json.loads(r.content) for r in self.requests if r.url.path == f"/api{path}"]


def _use_fake_ouigo(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, Callable[[httpx.Request], httpx.Response]] | None = None,
) -> FakeOuigo:
    """Route the example's async HTTP client through a local fake transport."""
    backend = FakeOuigo(routes or {})
    async_client = httpx.AsyncClient
    transport = httpx.MockTransport(backend)

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return async_client(*args, **kwargs)

    monkeypatch.setattr(ouigo_server.httpx, "AsyncClient", client_factory)
    return backend


def _search_response(journeys: list[object]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, json={"outbound": journeys, "error": None})


def test_login_token_and_station_list_are_reused_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OUIGO_API_USERNAME", raising=False)
    monkeypatch.delenv("OUIGO_API_PASSWORD", raising=False)
    backend = _use_fake_ouigo(monkeypatch, {"/Sale/journeysearch": _search_response([])})
    tools = ouigo_server.OuigoTools()

    async def run() -> None:
        await tools.search_trains("MT1", "7171801", "2026-10-14")
        await tools.search_trains("Madrid", "Barcelona", "2026-10-15")

    asyncio.run(run())

    assert backend.bodies("/Token/login") == [
        {"username": "ouigo.web", "password": "SquirelWeb!2020"}
    ]
    assert [r.url.path for r in backend.requests].count("/api/Data/GetStations") == 1
    for request in backend.requests:
        assert request.headers["Origin"] == "https://ventas.ouigo.com"
        assert request.headers["Accept-Language"] == "es"
    data_requests = [r for r in backend.requests if r.url.path != "/api/Token/login"]
    assert {r.headers["Authorization"] for r in data_requests} == {"Bearer token-1"}


def test_login_credentials_can_be_overridden_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUIGO_API_USERNAME", "other.user")
    monkeypatch.setenv("OUIGO_API_PASSWORD", "other-secret")
    backend = _use_fake_ouigo(monkeypatch)

    asyncio.run(ouigo_server.OuigoTools().find_station("BAR"))

    assert backend.bodies("/Token/login") == [
        {"username": "other.user", "password": "other-secret"}
    ]


def test_expired_token_triggers_one_relogin_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    def search(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer token-1":
            return httpx.Response(401)
        return httpx.Response(200, json={"outbound": [], "error": None})

    backend = _use_fake_ouigo(monkeypatch, {"/Sale/journeysearch": search})

    assert asyncio.run(ouigo_server.OuigoTools().search_trains("MT1", "BAR", "2026-10-14")) == []
    assert backend.logins == 2
    assert len(backend.bodies("/Sale/journeysearch")) == 2


def test_repeated_unauthorized_responses_are_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _use_fake_ouigo(
        monkeypatch, {"/Calendar/prices": lambda request: httpx.Response(401)}
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            ouigo_server.OuigoTools().get_price_calendar("MT1", "BAR", "2026-10-14", "2026-10-15")
        )
    assert backend.logins == 2
    assert len(backend.bodies("/Calendar/prices")) == 2


def test_concurrent_calls_share_one_login(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _use_fake_ouigo(monkeypatch, {"/Sale/journeysearch": _search_response([])})
    tools = ouigo_server.OuigoTools()

    async def run() -> None:
        await asyncio.gather(
            tools.search_trains("MT1", "BAR", "2026-10-14"),
            tools.search_trains("MT1", "BAR", "2026-10-15"),
        )

    asyncio.run(run())

    assert backend.logins == 1


def test_concurrent_unauthorized_responses_share_one_relogin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def search(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401)
        return httpx.Response(200, json={"outbound": [], "error": None})

    backend = _use_fake_ouigo(monkeypatch, {"/Sale/journeysearch": search})
    tools = ouigo_server.OuigoTools()
    tools._token = "stale"

    async def run() -> None:
        await asyncio.gather(
            tools.search_trains("MT1", "BAR", "2026-10-14"),
            tools.search_trains("MT1", "BAR", "2026-10-15"),
        )

    asyncio.run(run())

    assert backend.logins == 1
    searches = [r for r in backend.requests if r.url.path == "/api/Sale/journeysearch"]
    assert [r.headers["Authorization"] for r in searches].count("Bearer token-1") == 2


def test_find_station_ignores_case_and_accents(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_fake_ouigo(monkeypatch)

    result = asyncio.run(ouigo_server.OuigoTools().find_station("CORDOBA"))

    assert result == [
        {
            "code": "7150500",
            "name": "Córdoba - Julio Anguita",
            "synonyms": ["Córdoba"],
            "connected_station_codes": ["MT1"],
        }
    ]


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("malaga", "7154413"),
        ("Madrid", "MT1"),
        ("mal", "7154413"),
        ("mat", "7160000"),
        ("7160000", "7160000"),
        (" cor ", "7150500"),
    ],
)
def test_station_names_resolve_to_codes(
    monkeypatch: pytest.MonkeyPatch, origin: str, expected: str
) -> None:
    backend = _use_fake_ouigo(monkeypatch, {"/Sale/journeysearch": _search_response([])})

    asyncio.run(ouigo_server.OuigoTools().search_trains(origin, "barcelona", "2026-10-14"))

    [body] = backend.bodies("/Sale/journeysearch")
    assert (body["origin"], body["destination"]) == (expected, "7171801")


@pytest.mark.parametrize(
    ("origin", "message"),
    [
        ("ma", r"Ambiguous OUIGO station for origin: ma; use one of .*Madrid.*Málaga"),
        ("Paris", "Unknown OUIGO station for origin: Paris"),
    ],
)
def test_ambiguous_or_unknown_stations_are_rejected(
    monkeypatch: pytest.MonkeyPatch, origin: str, message: str
) -> None:
    backend = _use_fake_ouigo(monkeypatch)

    with pytest.raises(ValueError, match=message):
        asyncio.run(ouigo_server.OuigoTools().search_trains(origin, "BAR", "2026-10-14"))
    assert backend.bodies("/Sale/journeysearch") == []


def test_search_trains_shapes_journeys_and_repeats_passengers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _use_fake_ouigo(monkeypatch, {"/Sale/journeysearch": _search_response([JOURNEY])})

    result = asyncio.run(ouigo_server.OuigoTools().search_trains("MT1", "BAR", "2026-10-14", 2))

    assert backend.bodies("/Sale/journeysearch") == [
        {
            "origin": "MT1",
            "destination": "7171801",
            "outbound_date": "2026-10-14",
            "passengers": [{"type": "A", "disability_type": "NH"}] * 2,
        }
    ]
    assert result == [
        {
            "trains": ["06471"],
            "departure": "2026-10-14T06:22:00+02:00",
            "departure_station": "Madrid - Puerta de Atocha - Almudena Grandes",
            "arrival": "2026-10-14T09:45:00+02:00",
            "arrival_station": "Barcelona - Sants",
            "duration_minutes": 203,
            "price": 33.0,
            "full": False,
            "remaining_seats": None,
            "packages": {"Ouigo PLUS": 42.0, "Ouigo FULL": 50.0},
        }
    ]


def test_search_trains_returns_empty_list_when_no_trains_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_ouigo(
        monkeypatch,
        {
            "/Sale/journeysearch": lambda request: httpx.Response(
                200,
                json={"outbound": [], "blocs_cms_outbound": ["NO_RESULTS"], "error": None},
            )
        },
    )

    assert asyncio.run(ouigo_server.OuigoTools().search_trains("MT1", "BAR", "2026-10-14")) == []


def test_search_trains_rejects_malformed_journeys(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = {**JOURNEY, "arrival_station": None}
    _use_fake_ouigo(monkeypatch, {"/Sale/journeysearch": _search_response([broken])})

    with pytest.raises(ValueError, match="missing arrival_station"):
        asyncio.run(ouigo_server.OuigoTools().search_trains("MT1", "BAR", "2026-10-14"))


def test_get_price_calendar_returns_daily_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _use_fake_ouigo(
        monkeypatch,
        {
            "/Calendar/prices": lambda request: httpx.Response(
                200,
                json=[
                    {"date": "2026-10-14", "price": 33.0, "is_promo": False},
                    {"date": "2026-10-15", "price": 19, "is_promo": True},
                    {"date": "2026-10-16", "price": None, "is_promo": False},
                ],
            )
        },
    )

    result = asyncio.run(
        ouigo_server.OuigoTools().get_price_calendar("Madrid", "BAR", "2026-10-14", "2026-10-16")
    )

    assert backend.bodies("/Calendar/prices") == [
        {
            "origin": "MT1",
            "destination": "7171801",
            "begin": "2026-10-14",
            "end": "2026-10-16",
            "direction": "outbound",
            "passengers": [{"type": "A", "disability_type": "NH"}],
        }
    ]
    assert result == [
        {"date": "2026-10-14", "price": 33.0, "is_promo": False},
        {"date": "2026-10-15", "price": 19.0, "is_promo": True},
        {"date": "2026-10-16", "price": None, "is_promo": False},
    ]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda t: t.search_trains("MT1", "BAR", "14/10/2026"), "date must be a date"),
        (lambda t: t.search_trains("MT1", "BAR", "20261014"), "date must be a date"),
        (lambda t: t.search_trains("MT1", "BAR", "2026-10-14", 0), "adults must be"),
        (lambda t: t.search_trains("MT1", "BAR", "2026-10-14", 10), "adults must be"),
        (lambda t: t.search_trains(" ", "BAR", "2026-10-14"), "origin must not be empty"),
        (lambda t: t.find_station("  "), "query must not be empty"),
        (
            lambda t: t.get_price_calendar("MT1", "BAR", "2026-10-14", "2026-12-15"),
            "span at most 62 days",
        ),
        (
            lambda t: t.get_price_calendar("MT1", "BAR", "2026-10-14", "2026-10-13"),
            "on or after start_date",
        ),
    ],
)
def test_invalid_input_is_rejected_before_searching(
    monkeypatch: pytest.MonkeyPatch, call: Callable[[Any], Any], message: str
) -> None:
    backend = _use_fake_ouigo(monkeypatch)

    with pytest.raises(ValueError, match=message):
        asyncio.run(call(ouigo_server.OuigoTools()))
    assert [r.url.path for r in backend.requests if "Sale" in r.url.path] == []
    assert [r.url.path for r in backend.requests if "Calendar" in r.url.path] == []


def test_upstream_http_errors_are_not_silently_converted_to_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_ouigo(
        monkeypatch, {"/Sale/journeysearch": lambda request: httpx.Response(503, json={})}
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(ouigo_server.OuigoTools().search_trains("MT1", "BAR", "2026-10-14"))


def test_build_app_is_explicit_and_registers_only_ouigo_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected_app = object()

    monkeypatch.setattr(ouigo_server, "auth_from_env", lambda: None)

    def fake_create_app(
        mcp: Any, *, allow_anonymous: bool = False, cors_origins: Any = None
    ) -> object:
        captured["mcp"] = mcp
        captured["allow_anonymous"] = allow_anonymous
        return expected_app

    monkeypatch.setattr(ouigo_server, "create_app", fake_create_app)

    assert not hasattr(ouigo_server, "app")
    assert ouigo_server.build_app() is expected_app
    assert captured["allow_anonymous"] is True
    tools = asyncio.run(captured["mcp"].list_tools())
    assert {tool.name for tool in tools} == {"find_station", "search_trains", "get_price_calendar"}
