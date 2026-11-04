
Loading older messages…
[07:15] Object: Searching nearby FM internet radio streams for area code 510...
[07:15] Object: Station search failed: ﷐
I found the problem! This time it's in the Second Life LSL script, not your Render service.

The strange character ﷐ is Second Life's JSON_INVALID indicator. 

Second Life Wiki
+1

There were two mistakes in my previous script:

It was checking the service's JSON true response incorrectly.

It was using text instead of integer indexes when reading the station list.

I've corrected both.

LSL corrections completed
Download the corrected script
VixinVenom Area Radio — Corrected LSL

Install it
Open the downloaded script in Notepad.

Copy everything.

Open your radio in Second Life.

Go to Contents.

Open the existing radio script.

Replace everything with the new version.

Click Save.

Important: Keep your existing GitHub and Render setup. You do not need to redeploy anything for this correction.

Then test area code 775 and select FM STATIONS.

Tell me exactly what appears. We're getting closer to having your radio working!

VixinVenom's 'Object'
No matching stations were found for this area code and category. Try the other category or another area code.
Okay, we're getting a valid response now, but the search is finding zero stations. This is a problem with the way I originally configured the station search, not something you're doing wrong.

I found the issue.

Why it's happening
Radio Browser uses its tags field for music genres and topics, such as rock, jazz, and news. It does not reliably use AM and FM to identify broadcast frequencies. My original service incorrectly searched for AM/FM tags, which can produce empty results. 

docs.radio-browser.info
+1

I've prepared another correction
Updated service — Version 2
The new version:

Removes the incorrect AM/FM tag filter.

Adds a Reno-area location for area code 775.

Adds an Oakland-area location for area code 510.

Expands searches from 100 to 500 miles.

Tries MP3 streams first, then other available formats.

Keeps your existing Render address.

Download the updated files:

1. Updated Python service
Download app.py

2. Updated Second Life script
Download LSL Version 3

3. Complete updated package
Download ZIP package

What to do next
First, replace app.py in GitHub:

Open your GitHub repository.

Click app.py.

Click the pencil icon.

Replace all the old code with the new downloaded app.py contents.

Click Commit changes.

Render should automatically redeploy.

Then test this address:

Test Reno FM search
We're making progress, but one important limitation remains: these are nearby internet radio streams, not verified terrestrial AM/FM broadcasts. The AM VIEW and FM VIEW can show the same stations.

Let's get the station list working first, then we'll test actual playback in Second Life.


app.py
Code
there is only 1 station on 775 and its not good lol... i want all stations from any area code :(
YES! That's exactly what we need to fix. I found the reason you're only getting one station.

The previous version had three restrictions:

It searched only within 100 miles first.

It stopped searching as soon as it found even one station.

It preferred MP3 streams exclusively before trying other formats.

That means it was cutting off most of the available stations.

I'm changing the search to cover the entire state associated with the area code, plus nearby stations from surrounding states. It will also have multiple pages so you can browse a much larger selection.

I've made a much bigger improvement to the radio search.

VixinVenom Radio — Version 3 Update
What's changed
For area code 775, for example, the radio will now search:

All indexed stations listed for Nevada.

Additional stations within 500 miles, including surrounding states.

MP3 stations first, followed by other available stream formats.

Duplicate stations are removed.

Broken stations are excluded.

Up to 8 stations are displayed per page, with NEXT and PREV buttons.

The Radio Browser directory supports searching by state and country, with pagination. 

docs.radio-browser.info
+1

Download the updated files
1. Complete updated package
Download VixinVenom Radio Update V3 (ZIP)

2. Updated station lookup service
Download app.py

3. Updated Second Life script
Download LSL Version 4

Let's install it
First: Update GitHub

Open your GitHub repository.

Click app.py.

Click the pencil icon.

Replace all the code with the downloaded new app.py.

Click Commit changes.

Render should automatically redeploy.

Second: Update Second Life

Replace your existing radio script with LSL Version 4. This is important because the new version supports server-side NEXT/PREV paging.

Your settings are preserved:

Owner-only: TRUE

Your existing Render URL.

Play and Stop.

Area-code entry.

Test the new search
After Render says Live, open:

Test area code 775
You should see a station count, up to 8 stations, and whether more pages are available.

One important distinction: these are internet radio streams, not a verified list of terrestrial AM and FM frequencies. The current directory cannot reliably separate actual AM broadcasters from FM broadcasters.

I tested the updated code's syntax and simulated the multi-page station results. Live station totals still need to be confirmed against the online directory.


app.py
Code
says cant load file
Sorry about that! I repackaged the files and verified that the ZIP opens correctly.

Try these individual downloads instead:

Download Updated Radio Service – app.py

Download Updated Second Life Script – LSL v4

Download Complete Radio Package – ZIP

I'd recommend trying the first link.

Important: Open the text file with Notepad, but don't replace anything yet. We're going to make sure you have the right files before changing your working radio again.


VixinVenom_Radio_app_v3.txt
Document

VixinVenom_Radio_LSL_v4.txt
Document
For the radio station search, here's one app-building option for updating the tool.






"""VixinVenom Area Radio station lookup service.

Area codes resolve to an approximate state/province. Searches include the
whole state/province plus nearby geotagged stations, then serve results in
small pages suitable for Second Life LSL HTTP limits.
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
USER_AGENT = "VixinVenomAreaRadio/2.0 (Second Life internet radio)"
AREA_API = "https://areacodecheck.com/api/phone/{}"
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
RADIO_BROWSER_DISCOVERY = "https://all.api.radio-browser.info/json/servers"
RADIO_BROWSER_FALLBACKS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://fr1.api.radio-browser.info",
]
REQUEST_TIMEOUT = 18
AREA_CACHE_SECONDS = 24 * 60 * 60
STATION_CACHE_SECONDS = 15 * 60
AREA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
STATION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
MIRROR_CACHE: tuple[float, list[str]] | None = None
PAGE_SIZE = 8

AREA_OVERRIDES: dict[str, dict[str, Any]] = {
    "775": {"location": "Reno / Northern Nevada", "state": "Nevada", "latitude": 39.5296, "longitude": -119.8138, "country_code": "US"},
    "702": {"location": "Las Vegas / Southern Nevada", "state": "Nevada", "latitude": 36.1699, "longitude": -115.1398, "country_code": "US"},
    "725": {"location": "Las Vegas / Southern Nevada", "state": "Nevada", "latitude": 36.1699, "longitude": -115.1398, "country_code": "US"},
    "510": {"location": "Oakland / East Bay", "state": "California", "latitude": 37.8044, "longitude": -122.2712, "country_code": "US"},
}


def get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
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


def radio_browser_json(path: str, *, params: dict[str, Any] | None = None) -> Any:
    errors: list[str] = []
    for base in radio_browser_mirrors():
        try:
            response = requests.get(base + path, params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "unknown"
            errors.append(f"{base}: HTTP {code}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{base}: {type(exc).__name__}")
    raise RuntimeError("All Radio Browser mirrors failed. " + "; ".join(errors[:4]))


def resolve_area_code(area: str) -> dict[str, Any]:
    now = time.time()
    cached = AREA_CACHE.get(area)
    if cached and cached[0] > now:
        return cached[1]
    if area in AREA_OVERRIDES:
        result = AREA_OVERRIDES[area]
        AREA_CACHE[area] = (now + AREA_CACHE_SECONDS, result)
        return result

    payload = get_json(AREA_API.format(area + "2345678"))
    data = payload.get("data") or {}
    region = str(data.get("area_location") or "").strip()
    area_country = str(data.get("area_country") or "").strip()
    if not region:
        raise ValueError("The area-code directory did not return a state or province for that code.")
    country_code = "CA" if "canada" in area_country.casefold() else "US"
    geo = get_json(GEOCODING_API, params={"name": region, "count": 30, "language": "en", "format": "json"})
    results = geo.get("results") or []
    candidates = [x for x in results if str(x.get("country_code") or "").upper() == country_code]
    if not candidates:
        raise ValueError(f"Could not resolve {region} to a {country_code} province/state.")
    chosen = candidates[0]
    region_fold = region.casefold()
    for candidate in candidates:
        if str(candidate.get("name") or "").casefold() == region_fold or str(candidate.get("admin1") or "").casefold() == region_fold:
            chosen = candidate
            break
    state = str(chosen.get("admin1") or chosen.get("name") or region).strip()
    result = {
        "location": f"{state}, {chosen.get('country') or area_country}".strip(", "),
        "state": state,
        "latitude": float(chosen["latitude"]),
        "longitude": float(chosen["longitude"]),
        "country_code": country_code,
    }
    AREA_CACHE[area] = (now + AREA_CACHE_SECONDS, result)
    return result


def normalize_stations(rows: Any) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    seen: set[str] = set()
    for station in rows if isinstance(rows, list) else []:
        uuid = str(station.get("stationuuid") or "").strip()
        name = re.sub(r"\s+", " ", str(station.get("name") or "")).strip()
        if not uuid or not name or uuid in seen:
            continue
        seen.add(uuid)
        compact.append({"name": name[:72], "uuid": uuid})
    return compact


def build_area_station_list(area: str, place: dict[str, Any]) -> dict[str, Any]:
    """Combine all indexed stations in the area-code state with nearby streams.

    The old code stopped as soon as it found even one station in a 100-mile
    search. This version always gathers the state/province result set and a
    500-mile geo-tagged supplement before de-duplicating and sorting.
    """
    cache_key = area
    now = time.time()
    cached = STATION_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    country = place["country_code"].upper()
    state = place.get("state") or ""
    combined: dict[str, dict[str, Any]] = {}
    state_count = 0
    geo_count = 0

    if state:
        state_rows = radio_browser_json("/json/stations/search", params={
            "countrycode": country, "state": state, "limit": 2000,
            "hidebroken": "true", "order": "clickcount", "reverse": "true",
        })
        for row in state_rows if isinstance(state_rows, list) else []:
            uuid = str(row.get("stationuuid") or "")
            if uuid and uuid not in combined and str(row.get("name") or "").strip():
                combined[uuid] = row
        state_count = len(combined)

    # Add nearby geotagged streams from adjoining states/provinces as well.
    geo_rows = radio_browser_json("/json/stations/search", params={
        "geo_lat": place["latitude"], "geo_long": place["longitude"],
        "geo_distance": 804672, "limit": 2000, "hidebroken": "true",
        "order": "clickcount", "reverse": "true",
    })
    for row in geo_rows if isinstance(geo_rows, list) else []:
        uuid = str(row.get("stationuuid") or "")
        if uuid and uuid not in combined and str(row.get("name") or "").strip():
            combined[uuid] = row
    geo_count = max(0, len(combined) - state_count)

    # A state query may be sparse because station directories rely on
    # voluntary metadata. If both state and nearby searches are empty, show
    # a clearly labelled country-wide directory rather than an empty radio.
    coverage = "state + nearby area"
    if not combined:
        national_rows = radio_browser_json(f"/json/stations/bycountrycodeexact/{country}", params={
            "limit": 2000, "hidebroken": "true", "order": "clickcount", "reverse": "true",
        })
        for row in national_rows if isinstance(national_rows, list) else []:
            uuid = str(row.get("stationuuid") or "")
            if uuid and uuid not in combined and str(row.get("name") or "").strip():
                combined[uuid] = row
        coverage = "country-wide fallback (station location not supplied by directory)"

    # Prefer healthy MP3 entries for viewer compatibility, but do not discard
    # other codecs solely because the directory metadata is incomplete.
    rows = list(combined.values())
    rows.sort(key=lambda s: (
        0 if str(s.get("codec") or "").casefold() == "mp3" else 1,
        -int(s.get("clickcount") or 0),
        str(s.get("name") or "").casefold(),
    ))
    result = {
        "area": area, "location": place["location"], "state": state,
        "country_code": country, "coverage": coverage,
        "state_station_count": state_count, "nearby_station_count": geo_count,
        "total_stations": len(rows), "stations": normalize_stations(rows),
    }
    STATION_CACHE[cache_key] = (now + STATION_CACHE_SECONDS, result)
    return result


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "VixinVenom Area Radio", "version": "2.0"})


@app.get("/stations")
def stations():
    area = (request.args.get("area") or "").strip()
    band = (request.args.get("band") or "").strip().lower()
    try:
        page = max(0, int(request.args.get("page", "0")))
    except ValueError:
        return jsonify(ok=False, error="Page must be a non-negative integer."), 400
    if not re.fullmatch(r"[2-9][0-9]{2}", area):
        return jsonify(ok=False, error="Enter a valid three-digit North American area code."), 400
    if band not in {"am", "fm"}:
        return jsonify(ok=False, error="Band must be am or fm."), 400
    try:
        place = resolve_area_code(area)
        full = build_area_station_list(area, place)
        rows = full["stations"]
        start = page * PAGE_SIZE
        batch = rows[start:start + PAGE_SIZE]
        return jsonify(
            ok=True, area=area, band=band, location=full["location"],
            state=full["state"], coverage=full["coverage"],
            total_stations=full["total_stations"], page=page,
            page_size=PAGE_SIZE, has_more=(start + PAGE_SIZE < len(rows)),
            band_note="These are internet radio streams. The directory does not reliably identify terrestrial AM/FM broadcast frequencies; AM and FM views browse the same area directory.",
            stations=batch,
        )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return jsonify(ok=False, error=f"An upstream lookup returned HTTP {status}. Check the Render service logs."), 502
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
