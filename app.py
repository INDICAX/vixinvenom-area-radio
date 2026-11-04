"""VixinVenom State Radio service v7.

Browse internet radio stations by country and state/province. No area codes,
location lookup, or AM/FM categorization is used. Stations are filtered using
Radio Browser health data and checked again when Play is selected.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

USER_AGENT = "VixinVenomStateRadio/7.0 (Second Life internet radio)"
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
MAX_STATION_AGE_DAYS = int(os.getenv("MAX_STATION_AGE_DAYS", "30"))

COUNTRIES = {
    "US": "United States",
    "CA": "Canada",
}
API_COUNTRIES = {
    "US": "United States of America",
    "CA": "Canada",
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia", "NT": "Northwest Territories", "NU": "Nunavut",
    "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}
SUBDIVISIONS = {f"US-{k}": v for k, v in US_STATES.items()}
SUBDIVISIONS.update({f"CA-{k}": v for k, v in CA_PROVINCES.items()})

STATE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
STATION_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
MIRROR_CACHE: tuple[float, list[str]] | None = None
FAILED_PLAY_CACHE: dict[str, float] = {}


def get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
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
                if re.fullmatch(r"[a-z0-9.-]+\.api\.radio-browser\.info", host) and not host.startswith("all."):
                    candidate = "https://" + host
                    if candidate not in mirrors:
                        mirrors.append(candidate)
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        pass

    for candidate in RADIO_BROWSER_FALLBACKS:
        if candidate not in mirrors:
            mirrors.append(candidate)

    MIRROR_CACHE = (now + 600, mirrors)
    return mirrors


def rb_json(path: str, params: dict[str, Any] | None = None) -> Any:
    errors: list[str] = []
    for base in radio_browser_mirrors():
        try:
            response = requests.get(
                base + path,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            errors.append(f"{base}: HTTP {code}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{base}: {type(exc).__name__}")
    raise RuntimeError("Radio Browser mirrors unavailable: " + "; ".join(errors[:3]))


def canonical_state(raw: str, country_code: str) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    # Radio directories sometimes append country/region text to the state field.
    text = text.split(",", 1)[0].strip()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    iso = text.upper()
    if iso in SUBDIVISIONS:
        return SUBDIVISIONS[iso]
    if "-" in iso and iso.split("-")[-1] in (US_STATES if country_code == "US" else CA_PROVINCES):
        code = iso.split("-")[-1]
        return (US_STATES if country_code == "US" else CA_PROVINCES)[code]

    mapping = US_STATES if country_code == "US" else CA_PROVINCES
    for code, name in mapping.items():
        if text.casefold() in {name.casefold(), code.casefold()}:
            return name
        if text.casefold().startswith(name.casefold() + " -"):
            return name
    return text[:60]


def station_state(row: dict[str, Any], country_code: str) -> str:
    iso = str(row.get("iso_3166_2") or "").strip().upper()
    if iso in SUBDIVISIONS and iso.startswith(country_code + "-"):
        return SUBDIVISIONS[iso]
    return canonical_state(str(row.get("state") or ""), country_code) or "State not listed"


def is_recently_healthy(row: dict[str, Any]) -> bool:
    value = row.get("lastcheckok")
    if str(value).strip().lower() not in {"1", "true"}:
        return False

    stamp = str(row.get("lastchecktime_iso8601") or row.get("lastchecktime") or "").strip()
    if not stamp:
        return False
    try:
        checked = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds() / 86400
        return -1 <= age <= MAX_STATION_AGE_DAYS
    except ValueError:
        return False


def clean_station(row: dict[str, Any], country_code: str) -> dict[str, Any] | None:
    uuid = str(row.get("stationuuid") or "").strip()
    if not uuid or FAILED_PLAY_CACHE.get(uuid, 0) > time.time():
        return None
    name = re.sub(r"\s+", " ", str(row.get("name") or "")).strip()
    if not name or not is_recently_healthy(row):
        return None
    try:
        if int(row.get("hls") or 0) == 1:
            return None
    except (TypeError, ValueError):
        return None

    stream = str(row.get("url_resolved") or row.get("url") or "").strip()
    parsed = urlparse(stream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    # Keep menu content ASCII and compact for Second Life llDialog.
    display_name = name.encode("ascii", "ignore").decode("ascii")
    display_name = re.sub(r"\s+", " ", display_name).strip() or "Unnamed station"
    return {
        "name": display_name[:48],
        "uuid": uuid,
        "state": station_state(row, country_code),
        "codec": str(row.get("codec") or "Unknown")[:12],
        "bitrate": int(row.get("bitrate") or 0),
    }


def get_state_list(country_code: str) -> list[dict[str, Any]]:
    now = time.time()
    cached = STATE_CACHE.get(country_code)
    if cached and cached[0] > now:
        return cached[1]

    country_name = COUNTRIES[country_code]
    rows = rb_json(
        "/json/states",
        params={
            "country": API_COUNTRIES[country_code],
            "hidebroken": "true",
            "order": "stationcount",
            "reverse": "true",
            "limit": 1000,
        },
    )
    if not isinstance(rows, list):
        raise RuntimeError("Radio Browser returned an unexpected state list.")

    seen: set[str] = set()
    states: list[dict[str, Any]] = []
    for row in rows:
        raw_name = str(row.get("name") or "").strip()
        if not raw_name:
            continue
        name = canonical_state(raw_name, country_code)
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            count = max(0, int(row.get("stationcount") or 0))
        except (TypeError, ValueError):
            count = 0
        if count:
            states.append({"name": name, "count": count})

    states.sort(key=lambda item: (-item["count"], item["name"].casefold()))
    STATE_CACHE[country_code] = (now + 1800, states)
    return states


def get_state_stations(country_code: str, state_name: str) -> list[dict[str, Any]]:
    cache_key = (country_code, state_name.casefold())
    now = time.time()
    cached = STATION_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    rows = rb_json(
        "/json/stations/search",
        params={
            "countrycode": country_code,
            "state": state_name,
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": 100000,
        },
    )
    if not isinstance(rows, list):
        raise RuntimeError("Radio Browser returned an unexpected station list.")

    by_uuid: dict[str, dict[str, Any]] = {}
    for row in rows:
        station = clean_station(row, country_code)
        if not station:
            continue
        # Prevent substring matches from leaking stations from a different state.
        if station["state"].casefold() != state_name.casefold():
            continue
        by_uuid[station["uuid"]] = station

    stations = list(by_uuid.values())
    stations.sort(key=lambda item: (-item["bitrate"], item["name"].casefold()))
    STATION_CACHE[cache_key] = (now + 900, stations)
    return stations


@app.get("/")
def home():
    return jsonify(
        ok=True,
        service="VixinVenom State Radio",
        version="7.0",
        endpoints=[
            "/health",
            "/countries",
            "/states?country=US",
            "/stations?country=US&state=Nevada&page=0",
            "/play?uuid=STATION_UUID",
        ],
    )


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        service="VixinVenom State Radio",
        version="7.0",
        browse_mode="country_then_state",
        dialog_page_size=PAGE_SIZE,
        station_check="Radio Browser recent health check plus live probe on play",
    )


@app.get("/countries")
def countries():
    return jsonify(ok=True, countries=[
        {"code": code, "name": name}
        for code, name in COUNTRIES.items()
    ])


@app.get("/states")
def states():
    country_code = (request.args.get("country") or "").strip().upper()
    if country_code not in COUNTRIES:
        return jsonify(ok=False, error="Choose US or CA."), 400
    try:
        available = get_state_list(country_code)
        return jsonify(
            ok=True,
            version="7.0",
            country=country_code,
            country_name=COUNTRIES[country_code],
            total_states=len(available),
            states=available,
        )
    except (requests.RequestException, RuntimeError, ValueError, TypeError) as exc:
        app.logger.exception("State list failed for country=%s", country_code)
        return jsonify(ok=False, error=str(exc) or "State lookup failed."), 502


@app.get("/stations")
def stations():
    country_code = (request.args.get("country") or "").strip().upper()
    state_name = re.sub(r"\s+", " ", (request.args.get("state") or "")).strip()
    try:
        page = max(0, int(request.args.get("page", "0")))
    except ValueError:
        return jsonify(ok=False, error="Page must be a non-negative integer."), 400

    if country_code not in COUNTRIES:
        return jsonify(ok=False, error="Choose US or CA."), 400
    if not state_name or len(state_name) > 80:
        return jsonify(ok=False, error="Choose a state or province."), 400

    try:
        canonical = canonical_state(state_name, country_code)
        known_states = {item["name"].casefold() for item in get_state_list(country_code)}
        if canonical.casefold() not in known_states:
            return jsonify(ok=False, error="That state or province is not in the current directory."), 404

        catalog = get_state_stations(country_code, canonical)
        start = page * PAGE_SIZE
        batch = catalog[start:start + PAGE_SIZE]
        return jsonify(
            ok=True,
            version="7.0",
            country=country_code,
            country_name=COUNTRIES[country_code],
            state=canonical,
            total_stations=len(catalog),
            page=page,
            page_size=PAGE_SIZE,
            has_more=(start + PAGE_SIZE < len(catalog)),
            stations=batch,
            note="Internet radio streams, not verified terrestrial AM/FM broadcasts.",
        )
    except (requests.RequestException, RuntimeError, ValueError, TypeError) as exc:
        app.logger.exception("Station page failed country=%s state=%s", country_code, state_name)
        return jsonify(ok=False, error=str(exc) or "Station search failed."), 502


def live_probe(stream_url: str) -> tuple[bool, str, str]:
    response = None
    try:
        response = requests.get(
            stream_url,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Icy-MetaData": "0", "Range": "bytes=0-0"},
            timeout=(PLAY_CONNECT_TIMEOUT, PLAY_READ_TIMEOUT),
        )
        if response.status_code >= 400:
            return False, f"Stream server returned HTTP {response.status_code}.", ""
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if any(marker in content_type for marker in ("text/html", "application/json", "text/plain")):
            return False, "Station returned a web page or text instead of audio.", ""
        # Headers alone are not enough: read one byte to verify that the server
        # actually begins sending a stream. The read timeout bounds the wait.
        first_byte = response.raw.read(1, decode_content=True)
        if not first_byte:
            return False, "Station connected but did not send audio data.", ""
        final_url = response.url or stream_url
        parsed = urlparse(final_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "Station redirected to an invalid URL.", ""
        return True, "Stream accepted the connection.", final_url
    except Exception as exc:
        return False, f"Station did not accept the connection check ({type(exc).__name__}).", ""
    finally:
        if response is not None:
            response.close()


@app.get("/play")
def play():
    uuid = (request.args.get("uuid") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return jsonify(ok=False, error="Invalid station ID."), 400
    if FAILED_PLAY_CACHE.get(uuid, 0) > time.time():
        return jsonify(ok=False, error="This station recently failed playback. Choose another."), 409

    try:
        records = rb_json("/json/stations/byuuid", {"uuids": uuid})
        if not records:
            return jsonify(ok=False, error="Station is no longer listed."), 404
        record = records[0]
        country_code = str(record.get("countrycode") or "US").upper()
        if not is_recently_healthy(record) or int(record.get("hls") or 0) == 1:
            FAILED_PLAY_CACHE[uuid] = time.time() + 21600
            return jsonify(ok=False, error="Directory health check is old or station is offline. Choose another."), 409

        stream_url = str(record.get("url_resolved") or record.get("url") or "").strip()
        parsed = urlparse(stream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            FAILED_PLAY_CACHE[uuid] = time.time() + 21600
            return jsonify(ok=False, error="Station has no usable direct HTTP audio URL."), 502

        ok, message, final_url = live_probe(stream_url)
        if not ok:
            FAILED_PLAY_CACHE[uuid] = time.time() + 21600
            for key in list(STATION_CACHE):
                STATION_CACHE.pop(key, None)
            return jsonify(ok=False, error=message + " This station is hidden for 6 hours."), 409

        return jsonify(
            ok=True,
            name=record.get("name") or "Radio station",
            url=final_url,
            country=COUNTRIES.get(country_code, country_code),
            state=station_state(record, country_code),
            codec=record.get("codec") or "Unknown",
            bitrate=record.get("bitrate") or 0,
            checked_at=record.get("lastchecktime") or "unknown",
        )
    except (requests.RequestException, RuntimeError, ValueError, TypeError, IndexError) as exc:
        app.logger.exception("Station play check failed uuid=%s", uuid)
        return jsonify(ok=False, error=str(exc) or "Could not verify station stream."), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
