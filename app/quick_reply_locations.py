"""Validate saved pins locally; map links are never fetched or geocoded."""
import math
import re
from urllib.parse import parse_qs, unquote, urlsplit


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_PAIR = re.compile(rf"\s*({_NUMBER})\s*,\s*({_NUMBER})\s*")


def coordinates_from_maps_url(value: str) -> tuple[float, float]:
    value = value.strip()
    pair = _PAIR.fullmatch(value)
    if pair:
        return float(pair[1]), float(pair[2])
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    if host in {"maps.app.goo.gl", "goo.gl"}:
        raise ValueError("Open the short Google Maps link, then copy the full pin URL or its coordinates.")
    if url.scheme not in {"http", "https"} or url.username or url.password or not re.fullmatch(
        r"(?:www\.|maps\.)?google\.(?:com|[a-z]{2}|(?:com|co)\.[a-z]{2})", host
    ) or (not host.startswith("maps.") and not url.path.startswith("/maps")):
        raise ValueError("Enter a Google Maps pin link or latitude, longitude coordinates.")
    query = parse_qs(url.query)
    # Place IDs, routes and viewport centres do not reliably identify a pin.
    if query.get("query_place_id") or "/dir/" in url.path:
        raise ValueError("Copy the location's coordinates from Google Maps instead of a place-ID or directions link.")
    pins = re.findall(rf"!3d({_NUMBER})!4d({_NUMBER})(?=[!?&#]|$)", unquote(url.path + "?" + url.query))
    if len(set(pins)) == 1:
        return tuple(map(float, pins[0]))
    for key in ("query", "q"):
        values = query.get(key, [])
        if len(values) == 1 and (pair := _PAIR.fullmatch(values[0])):
            return float(pair[1]), float(pair[2])
    raise ValueError("This link does not contain a single pin's coordinates. In Google Maps, right-click or press the pin and copy its latitude and longitude.")


def normalize_location(location: dict, title: str | None) -> dict:
    if not title:
        raise ValueError("Give this saved location a name.")
    latitude, longitude = location.get("latitude"), location.get("longitude")
    link = str(location.get("maps_url") or "").strip()
    if link:
        if latitude is not None or longitude is not None:
            raise ValueError("Enter coordinates or a Google Maps link, not both.")
        latitude, longitude = coordinates_from_maps_url(link)
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        raise ValueError("Enter both latitude and longitude, or a Google Maps pin link.") from None
    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Enter valid latitude (-90 to 90) and longitude (-180 to 180).")
    name = str(location.get("name") or title).strip()
    address = str(location.get("address") or "").strip()
    if len(name) > 255 or len(address) > 1000 or any(ord(char) < 32 for char in name + address):
        raise ValueError("Use a location name up to 255 characters and an address up to 1000 characters, without control characters.")
    return dict(latitude=latitude, longitude=longitude, name=name, address=address)
