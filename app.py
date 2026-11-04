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

USER_AGENT = "VixinVenomAreaRadio/1.2 (Second Life in-world radio controller)"
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
    "510": {
        "location": "Oakland / East Bay, California, United States (approximate area-code center)",
        "latitude": 37.8044,
        "longitude": -122.2712,
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
