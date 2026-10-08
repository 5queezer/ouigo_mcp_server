"""Read-only OUIGO Spain train search MCP server.

This is an unofficial client of the JSON API behind OUIGO's web shop
(ventas.ouigo.com). It logs in with the public web-client credentials embedded
in that site, which ``OUIGO_API_USERNAME`` and ``OUIGO_API_PASSWORD`` override.
The API is undocumented and may change without notice. The tools only search
stations, trains, and fares; they never book. Set ``MCP_AUTH_MODE=demo`` for an
explicit local demo; the default server mode requires the GitHub OAuth
configuration documented by the project.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from pydantic import Field
from starlette.applications import Starlette

from mcp_server import create_app
from mcp_server.auth import auth_from_env

OUIGO_API = "https://mdw01.api-es.ouigo.com/api"
OUIGO_HEADERS = {
    "Origin": "https://ventas.ouigo.com",
    "Accept-Language": "es",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
}
DEFAULT_USERNAME = "ouigo.web"
DEFAULT_PASSWORD = "SquirelWeb!2020"
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_CALENDAR_DAYS = 62

StationQuery = Annotated[str, Field(min_length=1, description="Station name, synonym, or code")]
StationRef = Annotated[
    str,
    Field(min_length=1, description="Station code (e.g. MT1) or an unambiguous station name"),
]
IsoDate = Annotated[str, Field(description="Date as YYYY-MM-DD")]
Adults = Annotated[int, Field(ge=1, le=9, description="Number of adult passengers")]


def _normalized(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _parse_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        parsed = None
    # fromisoformat also accepts compact and week forms; insist on YYYY-MM-DD.
    if parsed is None or parsed.isoformat() != value:
        raise ValueError(f"{field_name} must be a date in YYYY-MM-DD format")
    return parsed


def _passengers(adults: int) -> list[dict[str, str]]:
    if not 1 <= adults <= 9:
        raise ValueError("adults must be between 1 and 9")
    return [{"type": "A", "disability_type": "NH"} for _ in range(adults)]


def _required_text(record: Mapping[str, Any], field_name: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"OUIGO result is missing {field_name}")
    return value


def _required_mapping(record: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = record.get(field_name)
    if not isinstance(value, Mapping):
        raise ValueError(f"OUIGO result is missing {field_name}")
    return value


def _price(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"OUIGO returned an invalid {field_name}")
    return float(value)


def _station_names(station: Mapping[str, Any]) -> list[str]:
    synonyms = station.get("synonyms") or []
    names = [station["name"], *synonyms, station.get("short_code") or ""]
    return [name for name in names if isinstance(name, str) and name]


async def _login(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/Token/login",
        json={
            "username": os.environ.get("OUIGO_API_USERNAME", DEFAULT_USERNAME),
            "password": os.environ.get("OUIGO_API_PASSWORD", DEFAULT_PASSWORD),
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("OUIGO login response must be an object")
    return _required_text(payload, "token")


def _journey_summary(journey: Mapping[str, Any]) -> dict[str, object]:
    departure = _required_mapping(journey, "departure_station")
    arrival = _required_mapping(journey, "arrival_station")
    departs = _required_text(departure, "departure_timestamp")
    arrives = _required_text(arrival, "arrival_timestamp")
    segments = journey.get("segments")
    packages = journey.get("packages") or []
    if not isinstance(segments, list) or not all(isinstance(s, Mapping) for s in segments):
        raise ValueError("OUIGO journey segments must be a list of objects")
    if not isinstance(packages, list) or not all(isinstance(p, Mapping) for p in packages):
        raise ValueError("OUIGO journey packages must be a list of objects")
    try:
        duration = datetime.fromisoformat(arrives) - datetime.fromisoformat(departs)
    except ValueError as exc:
        raise ValueError("OUIGO returned an invalid journey timestamp") from exc
    return {
        "trains": [_required_text(segment, "service_name") for segment in segments],
        "departure": departs,
        "departure_station": _required_text(departure, "name"),
        "arrival": arrives,
        "arrival_station": _required_text(arrival, "name"),
        "duration_minutes": int(duration.total_seconds() // 60),
        "price": _price(journey.get("price"), "price"),
        "full": journey.get("full") is True,
        "remaining_seats": journey.get("remaining_seats"),
        "packages": {
            _required_text(package, "name"): _price(package.get("price"), "package price")
            for package in packages
        },
    }


class OuigoTools:
    """Tool implementations sharing one cached API token and station list."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._stations: list[Mapping[str, Any]] | None = None

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(
            base_url=OUIGO_API,
            headers=OUIGO_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as client:
            if self._token is None:
                self._token = await _login(client)
            for attempt in range(2):
                response = await client.request(
                    method, path, json=body, headers={"Authorization": f"Bearer {self._token}"}
                )
                # The token expires server-side; log in again once and retry.
                if response.status_code != 401 or attempt:
                    break
                self._token = await _login(client)
            response.raise_for_status()
            return response.json()

    async def _station_list(self) -> list[Mapping[str, Any]]:
        if self._stations is None:
            payload = await self._call("GET", "/Data/GetStations")
            if not isinstance(payload, list) or not all(isinstance(s, Mapping) for s in payload):
                raise ValueError("OUIGO station response must be a list of objects")
            for station in payload:
                _required_text(station, "_u_i_c_station_code")
                _required_text(station, "name")
            self._stations = payload
        return self._stations

    async def _resolve(self, ref: str, field_name: str) -> str:
        ref = ref.strip()
        if not ref:
            raise ValueError(f"{field_name} must not be empty")
        stations = await self._station_list()
        wanted = _normalized(ref)
        for station in stations:
            if ref == station["_u_i_c_station_code"] or wanted == _normalized(
                str(station.get("short_code") or "")
            ):
                return station["_u_i_c_station_code"]
        # Hidden stations (individual Madrid terminals) share the "Madrid"
        # synonym with MT1, so names resolve against visible stations only.
        visible = [station for station in stations if station.get("hidden") is not True]
        exact = [s for s in visible if wanted in map(_normalized, _station_names(s))]
        matches = exact or [
            s for s in visible if any(wanted in _normalized(n) for n in _station_names(s))
        ]
        if len(matches) == 1:
            return matches[0]["_u_i_c_station_code"]
        if not matches:
            raise ValueError(f"Unknown OUIGO station for {field_name}: {ref}")
        options = ", ".join(f"{s['name']} ({s['_u_i_c_station_code']})" for s in matches)
        raise ValueError(f"Ambiguous OUIGO station for {field_name}: {ref}; use one of {options}")

    async def find_station(self, query: StationQuery) -> list[dict[str, object]]:
        """Find OUIGO stations by name, synonym, or short code, ignoring case and accents."""
        wanted = _normalized(query)
        if not wanted:
            raise ValueError("query must not be empty")
        return [
            {
                "code": station["_u_i_c_station_code"],
                "name": station["name"],
                "synonyms": station.get("synonyms") or [],
                "connected_station_codes": station.get("connected_stations") or [],
            }
            for station in await self._station_list()
            if any(wanted in _normalized(name) for name in _station_names(station))
        ]

    async def search_trains(
        self, origin: StationRef, destination: StationRef, date: IsoDate, adults: Adults = 1
    ) -> list[dict[str, object]]:
        """List OUIGO trains for one day with base fares and package prices.

        Prices are in euros for the whole party. An empty list means no trains run.
        """
        _parse_date(date, "date")
        passengers = _passengers(adults)
        payload = await self._call(
            "POST",
            "/Sale/journeysearch",
            {
                "origin": await self._resolve(origin, "origin"),
                "destination": await self._resolve(destination, "destination"),
                "outbound_date": date,
                "passengers": passengers,
            },
        )
        if not isinstance(payload, Mapping):
            raise ValueError("OUIGO search response must be an object")
        journeys = payload.get("outbound") or []
        if not isinstance(journeys, list) or not all(isinstance(j, Mapping) for j in journeys):
            raise ValueError("OUIGO search response outbound must be a list of objects")
        return [_journey_summary(journey) for journey in journeys]

    async def get_price_calendar(
        self,
        origin: StationRef,
        destination: StationRef,
        start_date: IsoDate,
        end_date: IsoDate,
        adults: Adults = 1,
    ) -> list[dict[str, object]]:
        """Return the lowest OUIGO fare per day, in euros; price is null when no trains run."""
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date")
        if not 0 <= (end - start).days < MAX_CALENDAR_DAYS:
            raise ValueError(
                f"end_date must be on or after start_date and span at most {MAX_CALENDAR_DAYS} days"
            )
        passengers = _passengers(adults)
        payload = await self._call(
            "POST",
            "/Calendar/prices",
            {
                "origin": await self._resolve(origin, "origin"),
                "destination": await self._resolve(destination, "destination"),
                "begin": start_date,
                "end": end_date,
                "direction": "outbound",
                "passengers": passengers,
            },
        )
        if not isinstance(payload, list) or not all(isinstance(d, Mapping) for d in payload):
            raise ValueError("OUIGO calendar response must be a list of objects")
        return [
            {
                "date": _required_text(day, "date"),
                "price": _price(day.get("price"), "price"),
                "is_promo": day.get("is_promo") is True,
            }
            for day in payload
        ]


def _new_server(auth: AuthProvider | None) -> FastMCP:
    server = FastMCP(
        "ouigo",
        instructions=(
            "Search OUIGO Spain high-speed train stations, timetables, and fares. "
            "Use find_station to look up station codes. These tools are read-only "
            "and never book tickets."
        ),
        auth=auth,
    )
    tools = OuigoTools()
    server.tool()(tools.find_station)
    server.tool()(tools.search_trains)
    server.tool()(tools.get_price_calendar)
    return server


def build_app() -> Starlette:
    """Build the ASGI application from the current authentication environment."""
    auth = auth_from_env()
    server = _new_server(auth)
    return create_app(server, allow_anonymous=auth is None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_app(), host="0.0.0.0", port=8080, log_level="info")
