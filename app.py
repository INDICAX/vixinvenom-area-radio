"""VixinVenom Area Radio service v6.

Searches Radio Browser by approximate area-code center, groups indexed stations
by state/province, filters stations whose most recent directory check is not OK,
and performs a lightweight live HTTP stream probe when a station is selected.
"""
from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
USER_AGENT = "VixinVenomAreaRadio/6.0 (Second Life internet radio)"
AREA_API = "https://areacodecheck.com/api/phone/{}"
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
RADIO_BROWSER_DISCOVERY = "https://all.api.radio-browser.info/json/servers"
RADIO_BROWSER_FALLBACKS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://fr1.api.radio-browser.info",
]
REQUEST_TIMEOUT = 25
PLAY_CONNECT_TIMEOUT = 5
PLAY_READ_TIMEOUT = 8
PAGE_SIZE = 6
SEARCH_RADIUS_KM = int(os.getenv("SEARCH_RADIUS_KM", "800"))
MAX_STATION_AGE_DAYS = int(os.getenv("MAX_STATION_AGE_DAYS", "14"))

AREA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
STATION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
COUNTRY_STATION_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
MIRROR_CACHE: tuple[float, list[str]] | None = None
FAILED_PLAY_CACHE: dict[str, float] = {}

AREA_OVERRIDES: dict[str, dict[str, Any]] = {
    "775": {"location": "Reno / Northern Nevada", "state": "Nevada", "latitude": 39.5296, "longitude": -119.8138, "country_code": "US"},
    "702": {"location": "Las Vegas / Southern Nevada", "state": "Nevada", "latitude": 36.1699, "longitude": -115.1398, "country_code": "US"},
    "725": {"location": "Las Vegas / Southern Nevada", "state": "Nevada", "latitude": 36.1699, "longitude": -115.1398, "country_code": "US"},
    "510": {"location": "Oakland / East Bay", "state": "California", "latitude": 37.8044, "longitude": -122.2712, "country_code": "US"},
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}
US_STATE_LOOKUP = {v.casefold(): v for v in US_STATES.values()}
US_STATE_LOOKUP.update({k.casefold(): v for k, v in US_STATES.items()})


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(url, params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def radio_browser_mirrors() -> list[str]:
    global MIRROR_CACHE
    now = time.time()
    if MIRROR_CACHE and MIRROR_CACHE[0] > now:
        return MIRROR_CACHE[1]
    mirrors: list[str] = []
    try:
        for item in get_json(RADIO_BROWSER_DISCOVERY):
            host = str(item.get("name") or "").strip().lower()
            if re.fullmatch(r"[a-z0-9.-]+\.api\.radio-browser\.info", host) and not host.startswith("all."):
                base = "https://" + host
                if base not in mirrors:
                    mirrors.append(base)
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        pass
    for base in RADIO_BROWSER_FALLBACKS:
        if base not in mirrors:
            mirrors.append(base)
    MIRROR_CACHE = (now + 600, mirrors)
    return mirrors


def rb_json(path: str, params: dict[str, Any] | None = None) -> Any:
    errors = []
    for base in radio_browser_mirrors():
        try:
            r = requests.get(base + path, params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            errors.append(f"{base}: HTTP {exc.response.status_code if exc.response is not None else '?'}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{base}: {type(exc).__name__}")
    raise RuntimeError("Radio Browser mirrors unavailable: " + "; ".join(errors[:3]))


def resolve_area_code(area: str) -> dict[str, Any]:
    now = time.time()
    cached = AREA_CACHE.get(area)
    if cached and cached[0] > now:
        return cached[1]
    if area in AREA_OVERRIDES:
        place = AREA_OVERRIDES[area]
        AREA_CACHE[area] = (now + 86400, place)
        return place
    payload = get_json(AREA_API.format(area + "2345678"))
    data = payload.get("data") or {}
    region = str(data.get("area_location") or "").strip()
    country_name = str(data.get("area_country") or "").strip()
    if not region:
        raise ValueError("Area code could not be matched to a state or province.")
    country_code = "CA" if "canada" in country_name.casefold() else "US"
    geo = get_json(GEOCODING_API, {"name": region, "count": 30, "language": "en", "format": "json"})
    candidates = [x for x in (geo.get("results") or []) if str(x.get("country_code") or "").upper() == country_code]
    if not candidates:
        raise ValueError("Could not find coordinates for " + region)
    chosen = candidates[0]
    for candidate in candidates:
        if str(candidate.get("name") or "").casefold() == region.casefold() or str(candidate.get("admin1") or "").casefold() == region.casefold():
            chosen = candidate
            break
    state = str(chosen.get("admin1") or chosen.get("name") or region).strip()
    place = {"location": f"{state}, {chosen.get('country') or country_name}".strip(", "),
             "state": state, "latitude": float(chosen["latitude"]),
             "longitude": float(chosen["longitude"]), "country_code": country_code}
    AREA_CACHE[area] = (now + 86400, place)
    return place


def distance_km(a: float, b: float, c: float, d: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(x)))


def station_state(row: dict[str, Any], default_state: str = "") -> str:
    iso = str(row.get("iso_3166_2") or "").strip().upper()
    if iso.startswith("US-") and iso[3:] in US_STATES:
        return US_STATES[iso[3:]]
    raw = re.sub(r"\s+", " ", str(row.get("state") or "")).strip()
    if raw:
        raw = raw.split(",")[0].strip()
        folded = raw.casefold()
        if folded in US_STATE_LOOKUP:
            return US_STATE_LOOKUP[folded]
        # If a state string has a postal abbreviation suffix/prefix, use it.
        match = re.search(r"\b([A-Z]{2})\b", raw.upper())
        if match and match.group(1) in US_STATES:
            return US_STATES[match.group(1)]
        return raw[:48]
    return default_state or "State not listed"


def checked_recently(row: dict[str, Any]) -> bool:
    """Require Radio Browser's latest known health check to be successful and recent."""
    flag = row.get("lastcheckok")
    if str(flag).strip() not in {"1", "True", "true"}:
        return False
    stamp = str(row.get("lastchecktime_iso8601") or row.get("lastchecktime") or "").strip()
    if not stamp:
        return False
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400
        return -1 <= age_days <= MAX_STATION_AGE_DAYS
    except ValueError:
        return False


def clean_station(row: dict[str, Any], default_state: str = "") -> dict[str, Any] | None:
    uid = str(row.get("stationuuid") or "").strip()
    name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
    if not uid or not name or uid in FAILED_PLAY_CACHE and FAILED_PLAY_CACHE[uid] > time.time():
        return None
    if not checked_recently(row):
        return None
    if int(row.get("hls") or 0) == 1:
        return None  # parcel music players may not play HLS playlists reliably
    stream = str(row.get("url_resolved") or row.get("url") or "").strip()
    parsed = urlparse(stream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    # Use printable ASCII in dialog labels to avoid UTF-8 byte-limit surprises.
    ascii_name = name.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip()
    if not ascii_name:
        ascii_name = "Unnamed station"
    return {
        "name": ascii_name[:55],
        "uuid": uid,
        "state": station_state(row, default_state),
        "codec": str(row.get("codec") or "Unknown")[:12],
        "bitrate": int(row.get("bitrate") or 0),
        "lastcheckok": 1,
        "lastchecktime": str(row.get("lastchecktime") or "")[:19],
    }


def build_station_catalog(area: str, place: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    cached = STATION_CACHE.get(area)
    if cached and cached[0] > now:
        return cached[1]
    country = str(place["country_code"]).upper()
    origin = str(place.get("state") or "").strip()
    origin_fold = origin.casefold()
    lat, lon = float(place["latitude"]), float(place["longitude"])
    by_uuid: dict[str, dict[str, Any]] = {}

    # First: all verified indexed streams within the selected origin state.
    if origin:
        rows = rb_json("/json/stations/search", {
            "countrycode": country, "state": origin, "stateExact": "true",
            "hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": 100000,
        })
        if isinstance(rows, list):
            for row in rows:
                station = clean_station(row, origin)
                if station:
                    by_uuid[station["uuid"]] = station

    # Second: verified geotagged stations from the same country, locally radius-filtered.
    geo_rows = rb_json("/json/stations/search", {
        "countrycode": country, "has_geo_info": "true", "hidebroken": "true",
        "order": "clickcount", "reverse": "true", "limit": 100000,
    })
    if not isinstance(geo_rows, list):
        raise RuntimeError("Radio Browser returned an invalid station list.")
    for row in geo_rows:
        try:
            slat, slon = float(row.get("geo_lat")), float(row.get("geo_long"))
        except (TypeError, ValueError):
            continue
        if distance_km(lat, lon, slat, slon) > SEARCH_RADIUS_KM:
            continue
        station = clean_station(row, "")
        if station:
            by_uuid[station["uuid"]] = station

    stations = list(by_uuid.values())
    stations.sort(key=lambda x: (
        0 if x["state"].casefold() == origin_fold else 1,
        x["state"].casefold(),
        -x["bitrate"],
        x["name"].casefold(),
    ))
    groups: dict[str, list[dict[str, Any]]] = {}
    for station in stations:
        groups.setdefault(station["state"], []).append(station)
    state_names = sorted(groups, key=lambda s: (0 if s.casefold() == origin_fold else 1, s.casefold()))
    result = {
        "area": area, "location": place["location"], "origin_state": origin,
        "country_code": country, "radius_km": SEARCH_RADIUS_KM,
        "total_stations": len(stations),
        "filtered_states": len(groups),
        "states": [{"name": state, "count": len(groups[state])} for state in state_names],
        "by_state": groups,
    }
    STATION_CACHE[area] = (now + 600, result)
    return result


@app.get("/")
def home():
    return jsonify(ok=True, service="VixinVenom Area Radio", version="6.0",
                   endpoints=["/health", "/states?area=775&band=fm", "/stations?area=775&state=Nevada&band=fm&page=0", "/play?uuid=STATION_UUID"])


@app.get("/health")
def health():
    return jsonify(ok=True, service="VixinVenom Area Radio", version="6.0",
                   station_check="Radio Browser lastcheckok + recent check + live probe on play",
                   dialog_page_size=PAGE_SIZE)


@app.get("/states")
def states():
    area = (request.args.get("area") or "").strip()
    band = (request.args.get("band") or "fm").strip().lower()
    if not re.fullmatch(r"[2-9][0-9]{2}", area):
        return jsonify(ok=False, error="Enter a valid three-digit North American area code."), 400
    if band not in {"am", "fm"}:
        return jsonify(ok=False, error="Band must be am or fm."), 400
    try:
        place = resolve_area_code(area)
        catalog = build_station_catalog(area, place)
        return jsonify(ok=True, version="6.0", area=area, band=band,
                       location=catalog["location"], origin_state=catalog["origin_state"],
                       radius_km=catalog["radius_km"], total_stations=catalog["total_stations"],
                       states=catalog["states"],
                       note="Stations are grouped by directory state/province. AM/FM are browse labels; streams are internet radio.")
    except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
        app.logger.exception("State list failed for area=%s", area)
        return jsonify(ok=False, error=str(exc) or "State lookup failed."), 502


@app.get("/stations")
def stations():
    area = (request.args.get("area") or "").strip()
    band = (request.args.get("band") or "fm").strip().lower()
    state = (request.args.get("state") or "").strip()
    try:
        page = max(0, int(request.args.get("page", "0")))
    except ValueError:
        return jsonify(ok=False, error="Page must be a non-negative integer."), 400
    if not re.fullmatch(r"[2-9][0-9]{2}", area):
        return jsonify(ok=False, error="Enter a valid three-digit North American area code."), 400
    if band not in {"am", "fm"} or not state:
        return jsonify(ok=False, error="Select a state and AM/FM browse view."), 400
    try:
        place = resolve_area_code(area)
        catalog = build_station_catalog(area, place)
        state_map = {s.casefold(): s for s in catalog["by_state"]}
        canonical = state_map.get(state.casefold())
        if not canonical:
            return jsonify(ok=True, version="6.0", area=area, state=state, band=band,
                           total_stations=0, page=page, page_size=PAGE_SIZE,
                           has_more=False, stations=[])
        rows = catalog["by_state"][canonical]
        start = page * PAGE_SIZE
        batch = rows[start:start + PAGE_SIZE]
        return jsonify(ok=True, version="6.0", area=area, band=band,
                       location=catalog["location"], state=canonical,
                       total_stations=len(rows), page=page, page_size=PAGE_SIZE,
                       has_more=(start + PAGE_SIZE < len(rows)),
                       stations=batch)
    except (requests.RequestException, RuntimeError, ValueError, KeyError, TypeError) as exc:
        app.logger.exception("Station page failed for area=%s state=%s", area, state)
        return jsonify(ok=False, error=str(exc) or "Station search failed."), 502


def live_probe(stream_url: str) -> tuple[bool, str, str]:
    """Open the stream briefly to confirm it currently accepts an HTTP request."""
    response = None
    try:
        response = requests.get(stream_url, stream=True, allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Icy-MetaData": "0"},
            timeout=(PLAY_CONNECT_TIMEOUT, PLAY_READ_TIMEOUT))
        if response.status_code >= 400:
            return False, f"Stream server returned HTTP {response.status_code}.", ""
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            return False, "Station returned a web page instead of an audio stream.", ""
        final_url = response.url or stream_url
        parsed = urlparse(final_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "Station redirected to an invalid URL.", ""
        return True, "Stream accepted the connection.", final_url
    except requests.RequestException as exc:
        return False, f"Station did not accept a quick connection check ({type(exc).__name__}).", ""
    finally:
        if response is not None:
            response.close()


@app.get("/play")
def play():
    uuid = (request.args.get("uuid") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return jsonify(ok=False, error="Invalid station ID."), 400
    try:
        record = rb_json("/json/stations/byuuid", {"uuids": uuid})
        if not record or not checked_recently(record[0]) or int(record[0].get("hls") or 0) == 1:
            FAILED_PLAY_CACHE[uuid] = time.time() + 21600
            return jsonify(ok=False, error="Directory health check is not current or station is marked offline. Choose another station."), 409
        payload = rb_json("/json/url/" + uuid)
        stream_url = str(payload.get("url") or record[0].get("url_resolved") or "").strip()
        parsed = urlparse(stream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            FAILED_PLAY_CACHE[uuid] = time.time() + 21600
            return jsonify(ok=False, error="Station has no usable HTTP audio URL."), 502
        ok, message, final_url = live_probe(stream_url)
        if not ok:
            FAILED_PLAY_CACHE[uuid] = time.time() + 21600
            STATION_CACHE.clear()
            return jsonify(ok=False, error=message + " This station has been hidden for 6 hours; select another."), 409
        return jsonify(ok=True, name=record[0].get("name") or "Radio station", url=final_url,
                       codec=record[0].get("codec") or "Unknown", bitrate=record[0].get("bitrate") or 0,
                       checked_at=record[0].get("lastchecktime") or "unknown")
    except (requests.RequestException, RuntimeError, ValueError, TypeError, IndexError) as exc:
        app.logger.exception("Station play check failed uuid=%s", uuid)
        return jsonify(ok=False, error=str(exc) or "Could not verify station stream."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
