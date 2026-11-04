"""VixinVenom Area Radio lookup service.

Deploy this small Flask service publicly over HTTPS, then paste its base URL
into SERVICE_URL in VixinVenom_Area_Radio.lsl.
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

USER_AGENT = "VixinVenomAreaRadio/1.0 (Second Life in-world radio controller)"
AREA_API = "https://areacodecheck.com/api/phone/{}"
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
RADIO_BROWSER = "https://all.api.radio-browser.info"
REQUEST_TIMEOUT = 12
CACHE_SECONDS = 24 * 60 * 60
AREA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    api_key = os.getenv("AREACODECHECK_API_KEY", "").strip()
    if "areacodecheck.com" in url and api_key:
        headers["X-Api-Key"] = api_key
    response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def resolve_area_code(area: str) -> dict[str, Any]:
    now = time.time()
    cached = AREA_CACHE.get(area)
    if cached and cached[0] > now:
        return cached[1]

    # 555-0100 is a reserved fictional/example number block. Only the NPA
    # (first three digits) is being used to look up the area-code location.
    lookup_number = area + "5550100"
    payload = get_json(AREA_API.format(lookup_number))
    data = payload.get("data") or {}
    area_info = data.get("area_location")
    if isinstance(area_info, dict):
        location_text = ", ".join(str(x) for x in area_info.values() if x)
    else:
        location_text = str(area_info or "").strip()
    if not location_text:
        raise ValueError("The area-code directory did not return a location for that code.")

    parts = [p.strip() for p in location_text.split(",") if p.strip()]
    city_query = parts[0]
    region_hint = parts[1] if len(parts) > 1 else ""

    geo = get_json(GEOCODING_API, params={
        "name": city_query,
        "count": 10,
        "language": "en",
        "format": "json",
    })
    results = geo.get("results") or []
    if not results:
        raise ValueError(f"Could not find map coordinates for {location_text}.")

    chosen = results[0]
    if region_hint:
        hint = region_hint.casefold()
        for candidate in results:
            admin1 = str(candidate.get("admin1") or "").casefold()
            admin1_code = str(candidate.get("admin1_code") or "").casefold()
            if hint in admin1 or hint == admin1_code or admin1 in hint:
                chosen = candidate
                break

    country_code = str(chosen.get("country_code") or "").upper()
    if country_code not in {"US", "CA"}:
        raise ValueError("This radio currently supports US and Canadian area codes only.")

    result = {
        "location": ", ".join(x for x in [chosen.get("name"), chosen.get("admin1"), chosen.get("country")] if x),
        "latitude": float(chosen["latitude"]),
        "longitude": float(chosen["longitude"]),
        "country_code": country_code,
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
            "geo_distance": 160934,  # 100 miles from the approximate area-code center
            "tag": band,
            "codec": "MP3",
            "limit": 24,
            "hidebroken": "true",
            "order": "clicktrend",
            "reverse": "true",
        }
        found = get_json(RADIO_BROWSER + "/json/stations/search", params=params)
        compact = []
        seen = set()
        for station in found if isinstance(found, list) else []:
            uuid = str(station.get("stationuuid") or "")
            name = str(station.get("name") or "").strip()
            if not uuid or not name or uuid in seen:
                continue
            seen.add(uuid)
            compact.append({"name": name[:72], "uuid": uuid})
        return jsonify(ok=True, area=area, band=band, location=place["location"], stations=compact)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        if status == 429:
            return jsonify(ok=False, error="The area-code lookup service rate limit was reached. Try again later."), 429
        return jsonify(ok=False, error=f"A radio directory request failed (HTTP {status})."), 502
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        return jsonify(ok=False, error=str(exc) or "The area-code or station lookup failed."), 502


@app.get("/play")
def play():
    uuid = (request.args.get("uuid") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return jsonify(ok=False, error="Invalid station ID."), 400
    try:
        payload = get_json(RADIO_BROWSER + "/json/url/" + uuid)
        stream_url = str(payload.get("url") or "").strip()
        parsed = urlparse(stream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return jsonify(ok=False, error="The station did not provide a usable HTTP audio stream."), 502
        return jsonify(ok=True, name=payload.get("name") or "Radio station", url=stream_url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        return jsonify(ok=False, error=f"Could not retrieve station stream (HTTP {status})."), 502
    except requests.RequestException:
        return jsonify(ok=False, error="Could not reach the radio station directory."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
