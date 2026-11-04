"""VixinVenom Area Radio lookup service.

The Second Life LSL controller calls this HTTPS service to resolve a North
American area code, search Radio Browser, and retrieve a station's stream URL.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

USER_AGENT = "VixinVenomAreaRadio/1.1 (Second Life in-world radio controller)"
AREA_API = "https://areacodecheck.com/api/phone/{}"
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
# Radio Browser documentation says all.api is for discovering mirrors. Use
# the returned individual mirror hostnames for station requests.
RADIO_BROWSER_DISCOVERY = "https://all.api.radio-browser.info/json/servers"
RADIO_BROWSER_FALLBACKS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
]
REQUEST_TIMEOUT = 15
CACHE_SECONDS = 24 * 60 * 60
AREA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
MIRROR_CACHE: tuple[float, list[str]] | None = None

# Area codes identify broad numbering-plan regions, not exact towns. These
# well-known Nevada codes use a local approximate center to avoid resolving
# the state name "Nevada" to an unrelated town named Nevada.
AREA_OVERRIDES: dict[str, dict[str, Any]] = {
    "775": {
        "location": "Reno, Nevada, United States (approximate area-code center)",
        "latitude": 39.5296,
        "longitude": -119.8138,
        "country_code": "US",
    },
    "702": {
        "location": "Las Vegas, Nevada, United States (approximate area-code center)",
        "latitude": 36.1699,
        "longitude": -115.1398,
        "country_code": "US",
    },
    "725": {
        "location": "Las Vegas, Nevada, United States (approximate area-code center)",
        "latitude": 36.1699,
        "longitude": -115.1398,
        "country_code": "US",
    },
}


def get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    api_key = os.getenv("AREACODECHECK_API_KEY", "").strip()
    if "areacodecheck.com" in url and api_key:
        headers["X-Api-Key"] = api_key
    response = requests.get(
        url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def radio_browser_mirrors() -> list[str]:
    global MIRROR_CACHE
    now = time.time()
    if MIRROR_CACHE and MIRROR_CACHE[0] > now:
        return MIRROR_CACHE[1]

    mirrors: list[str] = []
    try:
        payload = get_json(RADIO_BROWSER_DISCOVERY)
        if isinstance(payload, list):
            for item in payload:
                host = str(item.get("name") or "").strip().lower()
                if (
                    re.fullmatch(r"[a-z0-9.-]+\.api\.radio-browser\.info", host)
                    and not host.startswith("all.")
                ):
                    base = "https://" + host
                    if base not in mirrors:
                        mirrors.append(base)
    except (requests.RequestException, ValueError, TypeError):
        # The documented fixed mirrors below let the service continue even if
        # the mirror-discovery endpoint is temporarily unavailable.
        pass

    for base in RADIO_BROWSER_FALLBACKS:
        if base not in mirrors:
            mirrors.append(base)

    MIRROR_CACHE = (now + 10 * 60, mirrors)
    return mirrors


def radio_browser_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    errors: list[str] = []
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
            status = exc.response.status_code if exc.response is not None else "unknown"
            errors.append(f"{base}: HTTP {status}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{base}: {type(exc).__name__}")
    raise RuntimeError(
        "All Radio Browser mirrors failed. " + "; ".join(errors[:4])
    )


def resolve_area_code(area: str) -> dict[str, Any]:
    now = time.time()
    cached = AREA_CACHE.get(area)
    if cached and cached[0] > now:
        return cached[1]

    if area in AREA_OVERRIDES:
        result = AREA_OVERRIDES[area]
        AREA_CACHE[area] = (now + CACHE_SECONDS, result)
        return result

    # AreaCodeCheck returns the area-code region (usually a state/province),
    # not an exact city. The synthetic number is used only to query its prefix.
    lookup_number = area + "2345678"
    payload = get_json(AREA_API.format(lookup_number))
    data = payload.get("data") or {}
    region = str(data.get("area_location") or "").strip()
    area_country = str(data.get("area_country") or "").strip()
    if not region:
        raise ValueError("The area-code directory did not return a region for that code.")

    expected_country = "CA" if "canada" in area_country.casefold() else "US"
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
    if not results:
        raise ValueError(f"Could not find coordinates for the area-code region {region}.")

    # Prefer a geocoder result located in the region returned by AreaCodeCheck.
    candidates = [
        item for item in results
        if str(item.get("country_code") or "").upper() == expected_country
    ]
    if not candidates:
        raise ValueError(f"Could not match {region} to a {expected_country} location.")

    chosen = candidates[0]
    region_fold = region.casefold()
    for candidate in candidates:
        name = str(candidate.get("name") or "").casefold()
        admin1 = str(candidate.get("admin1") or "").casefold()
        if name == region_fold or admin1 == region_fold:
            chosen = candidate
            break

    result = {
        "location": ", ".join(
            x for x in [chosen.get("name"), chosen.get("admin1"), chosen.get("country")] if x
        ) + " (approximate area-code region)",
        "latitude": float(chosen["latitude"]),
        "longitude": float(chosen["longitude"]),
        "country_code": expected_country,
    }
    AREA_CACHE[area] = (now + CACHE_SECONDS, result)
    return result


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "VixinVenom Area Radio"})


@app.get("/stations")
def stations():
    area = (request.args.get("area") or "").strip()
    band = (request.args.get("band") or "").strip().lower()
    if not re.fullmatch(r"[2-9][0-9]{2}", area):
        return jsonify(ok=False, error="Enter a valid three-digit North American area code."), 400
    if band not in {"am", "fm"}:
        return jsonify(ok=False, error="Band must be am or fm."), 400

    try:
        place = resolve_area_code(area)
        params = {
            "geo_lat": place["latitude"],
            "geo_long": place["longitude"],
            "geo_distance": 160934,  # 100 miles, in meters
            "tag": band,
            "codec": "MP3",
            "limit": 24,
            "hidebroken": "true",
            "order": "clicktrend",
            "reverse": "true",
        }
        found = radio_browser_json("/json/stations/search", params=params)
        compact = []
        seen = set()
        for station in found if isinstance(found, list) else []:
            uuid = str(station.get("stationuuid") or "")
            name = str(station.get("name") or "").strip()
            if not uuid or not name or uuid in seen:
                continue
            seen.add(uuid)
            compact.append({"name": name[:72], "uuid": uuid})
        return jsonify(
            ok=True,
            area=area,
            band=band,
            location=place["location"],
            stations=compact,
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return jsonify(ok=False, error=f"An upstream lookup returned HTTP {status}. Check service logs."), 502
    except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
        app.logger.exception("Station search failed for area=%s band=%s", area, band)
        return jsonify(ok=False, error=str(exc) or "The area-code or station lookup failed."), 502


@app.get("/play")
def play():
    uuid = (request.args.get("uuid") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return jsonify(ok=False, error="Invalid station ID."), 400
    try:
        payload = radio_browser_json("/json/url/" + uuid)
        stream_url = str(payload.get("url") or "").strip()
        parsed = urlparse(stream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return jsonify(ok=False, error="The station did not provide a usable HTTP audio stream."), 502
        return jsonify(ok=True, name=payload.get("name") or "Radio station", url=stream_url)
    except (requests.RequestException, RuntimeError, ValueError, TypeError) as exc:
        app.logger.exception("Station stream lookup failed for uuid=%s", uuid)
        return jsonify(ok=False, error=str(exc) or "Could not reach the radio station directory."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
