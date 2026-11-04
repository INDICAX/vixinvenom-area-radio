
"""VixinVenom Area Radio - geographic station search."""

from __future__ import annotations

import math
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

USER_AGENT = "VixinVenomAreaRadio/5.0"
AREA_API = "https://areacodecheck.com/api/phone/{}"
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
RADIO_BROWSER_DISCOVERY = "https://all.api.radio-browser.info/json/servers"

RADIO_BROWSER_FALLBACKS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://fr1.api.radio-browser.info",
]

REQUEST_TIMEOUT = 30
PAGE_SIZE = 8
AREA_CACHE_SECONDS = 86400
STATION_CACHE_SECONDS = 1200
COUNTRY_CACHE_SECONDS = 1200
SEARCH_RADIUS_KM = int(os.getenv("SEARCH_RADIUS_KM", "800"))

AREA_CACHE = {}
STATION_CACHE = {}
COUNTRY_STATION_CACHE = {}
MIRROR_CACHE = None

# Known North American area-code locations.
AREA_OVERRIDES = {
    "775": {
        "location": "Reno / Northern Nevada",
        "state": "Nevada",
        "latitude": 39.5296,
        "longitude": -119.8138,
        "country_code": "US",
    },
    "702": {
        "location": "Las Vegas / Southern Nevada",
        "state": "Nevada",
        "latitude": 36.1699,
        "longitude": -115.1398,
        "country_code": "US",
    },
    "725": {
        "location": "Las Vegas / Southern Nevada",
        "state": "Nevada",
        "latitude": 36.1699,
        "longitude": -115.1398,
        "country_code": "US",
    },
    "510": {
        "location": "Oakland / East Bay",
        "state": "California",
        "latitude": 37.8044,
        "longitude": -122.2712,
        "country_code": "US",
    },
}


def get_json(url, params=None):
    response = requests.get(
        url,
        params=params,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def radio_browser_mirrors():
    global MIRROR_CACHE

    now = time.time()

    if MIRROR_CACHE and MIRROR_CACHE[0] > now:
        return MIRROR_CACHE[1]

    mirrors = []

    try:
        payload = get_json(RADIO_BROWSER_DISCOVERY)

        if isinstance(payload, list):
            for item in payload:
                host = str(item.get("name") or "").strip().lower()

                if re.fullmatch(
                    r"[a-z0-9.-]+\.api\.radio-browser\.info",
                    host,
                ):
                    if not host.startswith("all."):
                        base = "https://" + host

                        if base not in mirrors:
                            mirrors.append(base)

    except (requests.RequestException, ValueError, TypeError):
        pass

    for base in RADIO_BROWSER_FALLBACKS:
        if base not in mirrors:
            mirrors.append(base)

    MIRROR_CACHE = (now + 600, mirrors)

    return mirrors


def radio_browser_json(path, params=None):
    errors = []

    for base in radio_browser_mirrors():
        try:
            response = requests.get(
                base + path,
                params=params,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )

            response.raise_for_status()
            return response.json()

        except requests.HTTPError as exc:
            code = (
                exc.response.status_code
                if exc.response is not None
                else "unknown"
            )
            errors.append(f"{base}: HTTP {code}")

        except (requests.RequestException, ValueError) as exc:
            errors.append(
                f"{base}: {type(exc).__name__}"
            )

    raise RuntimeError(
        "All Radio Browser mirrors failed: "
        + "; ".join(errors[:4])
    )


def resolve_area_code(area):
    now = time.time()

    cached = AREA_CACHE.get(area)

    if cached and cached[0] > now:
        return cached[1]

    if area in AREA_OVERRIDES:
        result = AREA_OVERRIDES[area]
        AREA_CACHE[area] = (
            now + AREA_CACHE_SECONDS,
            result,
        )
        return result

    payload = get_json(
        AREA_API.format(area + "2345678")
    )

    data = payload.get("data") or {}

    region = str(
        data.get("area_location") or ""
    ).strip()

    area_country = str(
        data.get("area_country") or ""
    ).strip()

    if not region:
        raise ValueError(
            "Area code could not be matched to a location."
        )

    country_code = (
        "CA"
        if "canada" in area_country.casefold()
        else "US"
    )

    geo = get_json(
        GEOCODING_API,
        params={
            "name": region,
            "count": 30,
            "language": "en",
            "format": "json",
        },
    )

    results = geo.get("results") or []

    candidates = [
        item for item in results
        if str(
            item.get("country_code") or ""
        ).upper() == country_code
    ]

    if not candidates:
        raise ValueError(
            "Could not find geographic coordinates for "
            + region
        )

    chosen = candidates[0]
    region_fold = region.casefold()

    for candidate in candidates:
        if (
            str(candidate.get("name") or "").casefold()
            == region_fold
            or str(candidate.get("admin1") or "").casefold()
            == region_fold
        ):
            chosen = candidate
            break

    state = str(
        chosen.get("admin1")
        or chosen.get("name")
        or region
    ).strip()

    result = {
        "location": (
            f"{state}, "
            f"{chosen.get('country') or area_country}"
        ).strip(", "),
        "state": state,
        "latitude": float(chosen["latitude"]),
        "longitude": float(chosen["longitude"]),
        "country_code": country_code,
    }

    AREA_CACHE[area] = (
        now + AREA_CACHE_SECONDS,
        result,
    )

    return result


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0088

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dlon / 2) ** 2
    )

    return (
        2
        * radius
        * math.asin(
            min(1.0, math.sqrt(a))
        )
    )


def country_geotagged_stations(country_code):
    now = time.time()

    cached = COUNTRY_STATION_CACHE.get(country_code)

    if cached and cached[0] > now:
        return cached[1]

    rows = radio_browser_json(
        "/json/stations/search",
        params={
            "countrycode": country_code,
            "has_geo_info": "true",
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": 100000,
        },
    )

    if not isinstance(rows, list):
        raise RuntimeError(
            "Unexpected response from Radio Browser."
        )

    COUNTRY_STATION_CACHE[country_code] = (
        now + COUNTRY_CACHE_SECONDS,
        rows,
    )

    return rows


def build_area_station_list(area, place):
    now = time.time()

    cached = STATION_CACHE.get(area)

    if cached and cached[0] > now:
        return cached[1]

    country = str(
        place["country_code"]
    ).upper()

    state = str(
        place.get("state") or ""
    )

    latitude = float(place["latitude"])
    longitude = float(place["longitude"])

    combined = {}

    # Get all indexed stations matching the state.
    if state:
        state_rows = radio_browser_json(
            "/json/stations/search",
            params={
                "countrycode": country,
                "state": state,
                "hidebroken": "true",
                "order": "clickcount",
                "reverse": "true",
                "limit": 100000,
            },
        )

        if isinstance(state_rows, list):
            for row in state_rows:
                uuid = str(
                    row.get("stationuuid") or ""
                ).strip()

                name = str(
                    row.get("name") or ""
                ).strip()

                if uuid and name:
                    combined[uuid] = (
                        row,
                        -1.0,
                    )

    # Search geotagged stations across the country.
    geo_rows = country_geotagged_stations(country)

    radius = float(SEARCH_RADIUS_KM)

    for row in geo_rows:
        uuid = str(
            row.get("stationuuid") or ""
        ).strip()

        name = str(
            row.get("name") or ""
        ).strip()

        try:
            station_lat = float(row.get("geo_lat"))
            station_lon = float(row.get("geo_long"))
        except (TypeError, ValueError):
            continue

        if not uuid or not name:
            continue

        distance = haversine_km(
            latitude,
            longitude,
            station_lat,
            station_lon,
        )

        if distance <= radius:
            if (
                uuid not in combined
                or combined[uuid][1] < 0
            ):
                combined[uuid] = (
                    row,
                    distance,
                )

    # Sort by popular stations, then distance,
    # preferring MP3 streams when otherwise comparable.
    rows_with_distance = list(combined.values())

    rows_with_distance.sort(
        key=lambda pair: (
            0 if pair[1] < 0 else 1,
            pair[1] if pair[1] >= 0 else 0,
            0 if str(
                pair[0].get("codec") or ""
            ).casefold() == "mp3" else 1,
            -int(
                pair[0].get("clickcount") or 0
            ),
            str(
                pair[0].get("name") or ""
            ).casefold(),
        )
    )

    stations = []
    seen = set()

    for row, distance in rows_with_distance:
        uuid = str(
            row.get("stationuuid") or ""
        ).strip()

        name = re.sub(
            r"\s+",
            " ",
            str(row.get("name") or ""),
        ).strip()

        if uuid and name and uuid not in seen:
            seen.add(uuid)

            stations.append({
                "name": name[:72],
                "uuid": uuid,
            })

    result = {
        "area": area,
        "location": place["location"],
        "state": state,
        "country_code": country,
        "coverage": (
            "state stations + geotagged stations within "
            + str(int(radius))
            + " km"
        ),
        "state_station_count": sum(
            1 for _, distance in rows_with_distance
            if distance < 0
        ),
        "nearby_station_count": sum(
            1 for _, distance in rows_with_distance
            if distance >= 0
        ),
        "total_stations": len(stations),
        "stations": stations,
    }

    STATION_CACHE[area] = (
        now + STATION_CACHE_SECONDS,
        result,
    )

    return result


@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "VixinVenom Area Radio",
        "message": "Radio service is online.",
        "endpoints": [
            "/health",
            "/stations?area=775&band=fm",
            "/play?uuid=STATION_UUID",
        ],
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "VixinVenom Area Radio",
        "version": "5.0",
    })


@app.get("/stations")
def stations():
    area = (
        request.args.get("area") or ""
    ).strip()

    band = (
        request.args.get("band") or ""
    ).strip().lower()

    try:
        page = max(
            0,
            int(request.args.get("page", "0")),
        )
    except ValueError:
        return jsonify(
            ok=False,
            error="Page must be a non-negative integer.",
        ), 400

    if not re.fullmatch(r"[2-9][0-9]{2}", area):
        return jsonify(
            ok=False,
            error="Enter a valid three-digit North American area code.",
        ), 400

    if band not in {"am", "fm"}:
        return jsonify(
            ok=False,
            error="Band must be am or fm.",
        ), 400

    try:
        place = resolve_area_code(area)

        full = build_area_station_list(
            area,
            place,
        )

        rows = full["stations"]

        start = page * PAGE_SIZE
        batch = rows[start:start + PAGE_SIZE]

        return jsonify(
            ok=True,
            version="5.0",
            area=area,
            band=band,
            location=full["location"],
            state=full["state"],
            coverage=full["coverage"],
            state_station_count=full["state_station_count"],
            nearby_station_count=full["nearby_station_count"],
            total_stations=full["total_stations"],
            page=page,
            page_size=PAGE_SIZE,
            has_more=(
                start + PAGE_SIZE < len(rows)
            ),
            band_note=(
                "These are internet radio streams. "
                "The directory does not reliably identify "
                "terrestrial AM/FM frequencies. Both views "
                "browse the same regional internet-station list."
            ),
            stations=batch,
        )

    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )

        app.logger.exception(
            "Upstream search failed for area %s",
            area,
        )

        return jsonify(
            ok=False,
            error=f"Radio Browser returned HTTP {status}.",
        ), 502

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:

        app.logger.exception(
            "Station search failed for area %s",
            area,
        )

        return jsonify(
            ok=False,
            version="5.0",
            error=str(exc) or "Station lookup failed.",
        ), 502


@app.get("/play")
def play():
    uuid = (
        request.args.get("uuid") or ""
    ).strip()

    if not re.fullmatch(
        r"[0-9a-fA-F-]{36}",
        uuid,
    ):
        return jsonify(
            ok=False,
            error="Invalid station ID.",
        ), 400

    try:
        payload = radio_browser_json(
            "/json/url/" + uuid
        )

        stream_url = str(
            payload.get("url") or ""
        ).strip()

        parsed = urlparse(stream_url)

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
        ):
            return jsonify(
                ok=False,
                error="Station did not provide a usable audio URL.",
            ), 502

        return jsonify(
            ok=True,
            name=payload.get("name") or "Radio station",
            url=stream_url,
        )

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        TypeError,
    ) as exc:

        app.logger.exception(
            "Station stream lookup failed for UUID %s",
            uuid,
        )

        return jsonify(
            ok=False,
            error=str(exc) or "Could not retrieve station stream.",
        ), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
    )
