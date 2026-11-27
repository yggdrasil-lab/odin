"""Odysseus MCP travel briefing client and Markdown formatter for Odin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
import urllib.error
import urllib.request


class OdysseusMcpError(RuntimeError):
    """Raised when an Odysseus MCP call fails or returns an error envelope."""


@dataclass(frozen=True)
class OdysseusTravelDigest:
    """Immutable travel briefing state returned for morning copilot synthesis."""

    status: str  # "ok", "none", "unconfigured", "error"
    trips: Tuple[Dict[str, Any], ...] = ()
    error: Optional[str] = None


def _sanitize_dashes(text: str) -> str:
    """Replace Unicode em/en dashes with ASCII hyphens."""
    return text.replace("\u2014", "-").replace("\u2013", "-")


def _coerce_date(value: date | str) -> date:
    """Coerce a date, datetime, or YYYY-MM-DD string into a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"Unsupported date value: {type(value).__name__}")


def _parse_optional_iso_date(raw: Any) -> Optional[date]:
    """Parse an optional ISO YYYY-MM-DD string, returning None if invalid."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def select_briefing_trips(
    trips: Sequence[Mapping[str, Any]],
    today: date | str,
    window_days: int = 7,
) -> List[Dict[str, Any]]:
    """Select active trips and booked trips starting within [0, window_days]."""
    today_date = _coerce_date(today)
    selected: List[Dict[str, Any]] = []
    for trip in trips:
        if not isinstance(trip, Mapping):
            continue
        status = str(trip.get("status") or "").strip().lower()
        if status == "active":
            selected.append(dict(trip))
            continue
        if status != "booked":
            continue
        start_date = _parse_optional_iso_date(trip.get("start_date"))
        if start_date is None:
            continue
        delta_days = (start_date - today_date).days
        if 0 <= delta_days <= window_days:
            selected.append(dict(trip))

    selected.sort(
        key=lambda item: (
            0 if str(item.get("status") or "").lower() == "active" else 1,
            str(item.get("start_date") or ""),
            str(item.get("id") or ""),
        )
    )
    return selected


def _post_json_rpc(req: urllib.request.Request, timeout: float) -> Any:
    """Execute an HTTP POST request and decode the JSON-RPC response body."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            detail = str(exc.reason)
        raise OdysseusMcpError(f"Odysseus MCP HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OdysseusMcpError(f"Odysseus MCP request failed: {exc}") from exc

    try:
        return json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise OdysseusMcpError(f"Invalid JSON from Odysseus MCP: {exc}") from exc


def _unwrap_mcp_tool_result(envelope: Any, tool_name: str) -> Any:
    """Validate a JSON-RPC 2.0 MCP tools/call response and decode result JSON."""
    if not isinstance(envelope, dict):
        raise OdysseusMcpError(f"Unexpected JSON-RPC response type for {tool_name}")
    if envelope.get("error") is not None:
        raise OdysseusMcpError(f"Odysseus MCP JSON-RPC error: {envelope['error']}")

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise OdysseusMcpError(f"Missing JSON-RPC result object for {tool_name}")

    content = result.get("content")
    text: Optional[str] = None
    if isinstance(content, list) and content and isinstance(content[0], dict):
        raw_text = content[0].get("text")
        if isinstance(raw_text, str):
            text = raw_text

    if result.get("isError"):
        raise OdysseusMcpError(f"Odysseus MCP tool {tool_name} error: {text or result}")
    if text is None:
        raise OdysseusMcpError(f"Odysseus MCP tool {tool_name} returned empty content")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OdysseusMcpError(f"Invalid tool JSON from {tool_name}: {exc}") from exc


def call_odysseus_mcp(
    mcp_url: str,
    token: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    timeout: float = 4.0,
) -> Any:
    """Invoke a stateless JSON-RPC 2.0 tool on the Odysseus MCP endpoint."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": dict(arguments)},
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Yggdrasil-Odin/1.0",
    }
    req = urllib.request.Request(mcp_url, data=body, headers=headers, method="POST")
    envelope = _post_json_rpc(req, timeout=timeout)
    return _unwrap_mcp_tool_result(envelope, tool_name)


def _extract_trips_list(raw_payload: Any) -> List[Mapping[str, Any]]:
    """Normalize odysseus_list_trips payload into a list of trip dicts."""
    if isinstance(raw_payload, list):
        return raw_payload
    if isinstance(raw_payload, dict) and isinstance(raw_payload.get("trips"), list):
        return raw_payload["trips"]
    raise OdysseusMcpError("odysseus_list_trips returned unexpected payload shape")


def _fetch_single_day_brief(
    mcp_url: str,
    token: str,
    trip: Mapping[str, Any],
    today_date: date,
    timeout: float,
) -> Dict[str, Any]:
    """Fetch and enrich a single trip's day brief via odysseus_get_day_brief."""
    trip_id = str(trip.get("id") or "").strip()
    if not trip_id:
        raise OdysseusMcpError("Odysseus trip entry missing id")
    status = str(trip.get("status") or "").strip().lower()
    target_date = (
        today_date.isoformat()
        if status == "active"
        else str(trip.get("start_date") or today_date.isoformat())
    )
    brief = call_odysseus_mcp(
        mcp_url,
        token,
        "odysseus_get_day_brief",
        {"trip_id": trip_id, "target_date": target_date},
        timeout=timeout,
    )
    if not isinstance(brief, dict):
        raise OdysseusMcpError("odysseus_get_day_brief returned non-object payload")

    enriched = dict(brief)
    enriched.setdefault("trip_id", trip_id)
    enriched.setdefault("trip_status", status)
    enriched.setdefault("target_date", target_date)
    if trip.get("slug") is not None:
        enriched.setdefault("trip_slug", trip.get("slug"))
    if trip.get("title") is not None:
        enriched.setdefault("trip_title", trip.get("title"))
    return enriched


def fetch_odysseus_travel_briefings(
    *,
    mcp_url: str,
    token: str,
    today: date | str,
    window_days: int = 7,
    timeout: float = 4.0,
) -> OdysseusTravelDigest:
    """Fetch Odysseus day briefs for active and near-term booked trips."""
    if not token or not token.strip():
        return OdysseusTravelDigest(status="unconfigured")
    if not mcp_url or not mcp_url.strip():
        return OdysseusTravelDigest(status="error", error="ODYSSEUS_MCP_URL is empty")

    today_date = _coerce_date(today)
    clean_token = token.strip()
    try:
        raw_trips = call_odysseus_mcp(
            mcp_url, clean_token, "odysseus_list_trips", {}, timeout=timeout
        )
        trips_list = _extract_trips_list(raw_trips)
        selected = select_briefing_trips(trips_list, today_date, window_days=window_days)
        if not selected:
            return OdysseusTravelDigest(status="none")
        briefs = tuple(
            _fetch_single_day_brief(mcp_url, clean_token, trip, today_date, timeout)
            for trip in selected
        )
        return OdysseusTravelDigest(status="ok", trips=briefs)
    except OdysseusMcpError as exc:
        return OdysseusTravelDigest(status="error", error=str(exc))


def _extract_hhmm(raw_local: Any) -> str:
    """Extract HH:MM wall-clock time from an ISO local datetime string."""
    if not isinstance(raw_local, str) or not raw_local.strip():
        return ""
    try:
        return datetime.fromisoformat(raw_local.strip()).strftime("%H:%M")
    except ValueError:
        return ""


def _format_time_with_tz(raw_local: Any, raw_tz: Any) -> str:
    """Format an ISO local datetime string as HH:MM with optional IANA timezone."""
    hhmm = _extract_hhmm(raw_local)
    if not hhmm:
        return ""
    tz = str(raw_tz or "").strip()
    return f"{hhmm} ({tz})" if tz else hhmm


def _format_leg_entry(entry: Any) -> str:
    """Render a compact summary for an Odysseus active_legs entry."""
    if not isinstance(entry, Mapping):
        return str(entry)
    name = str(entry.get("name") or "").strip()
    city = str(entry.get("primary_city") or "").strip()
    if name and city and city.lower() != name.lower():
        return f"{name} ({city})"
    return name or city or "Leg"


def _format_segment_entry(entry: Any, *, is_arrival: bool = False) -> str:
    """Render a compact summary for a departing or arriving booking segment."""
    if not isinstance(entry, Mapping):
        return str(entry)
    time_field, tz_field = (
        ("arrival_local", "arrival_tz") if is_arrival else ("departure_local", "departure_tz")
    )
    time_str = _format_time_with_tz(entry.get(time_field), entry.get(tz_field))
    day_offset = entry.get("day_offset")
    if is_arrival and time_str and isinstance(day_offset, int) and not isinstance(day_offset, bool) and day_offset > 0:
        time_str = f"{time_str} +{day_offset}d"

    service = str(entry.get("flight_or_service_number") or entry.get("carrier") or "").strip()
    booking_title = str(entry.get("booking_title") or "").strip()
    if service and booking_title and service.lower() not in booking_title.lower():
        label = f"{service} ({booking_title})"
    else:
        label = service or booking_title or "Segment"

    origin = str(entry.get("origin_code") or entry.get("origin_label") or "").strip()
    dest = str(entry.get("destination_code") or entry.get("destination_label") or "").strip()
    route = f"{origin} -> {dest}" if origin and dest else (origin or dest)

    ref = str(entry.get("confirmation_code") or "").strip()
    ref_part = f"Ref {ref}" if ref else ""
    parts = [p for p in (time_str, label, route, ref_part) if p]
    return " | ".join(parts)


def _format_itinerary_entry(entry: Any) -> str:
    """Render a compact summary for an Odysseus itinerary_items entry."""
    if not isinstance(entry, Mapping):
        return str(entry)
    time_str = _format_time_with_tz(entry.get("start_local"), entry.get("start_tz"))
    title = str(entry.get("title") or "Item").strip()
    place = str(entry.get("place_name") or "").strip()
    parts = [p for p in (time_str, title, place) if p]
    return " | ".join(parts)


_BRIEF_SECTIONS: Tuple[Tuple[str, str, int, Callable[[Any], str]], ...] = (
    ("active_legs", "Legs", 3, _format_leg_entry),
    ("departing_segments", "Departing", 3, lambda item: _format_segment_entry(item, is_arrival=False)),
    ("arriving_segments", "Arriving", 3, lambda item: _format_segment_entry(item, is_arrival=True)),
    ("itinerary_items", "Itinerary", 5, _format_itinerary_entry),
)


def _format_trip_brief_lines(brief: Mapping[str, Any]) -> List[str]:
    """Format a single Odysseus trip day brief into Markdown bullets."""
    title = str(brief.get("trip_title") or brief.get("title") or "Trip")
    target_date = str(brief.get("target_date") or "")
    days_out = brief.get("days_until_departure")
    if isinstance(days_out, int) and not isinstance(days_out, bool):
        timing = "Departing today" if days_out == 0 else (f"In {days_out}d" if days_out > 0 else "Active")
    else:
        timing = "Active" if brief.get("trip_status") == "active" else "Upcoming"

    meta = f"{timing}, {target_date}" if target_date else timing
    lines = [f"- **{title}** ({meta})"]

    for key, label, limit, formatter in _BRIEF_SECTIONS:
        raw_items = brief.get(key)
        if not isinstance(raw_items, list) or not raw_items:
            continue
        rendered = [formatter(item) for item in raw_items[:limit]]
        if len(raw_items) > limit:
            rendered.append(f"+{len(raw_items) - limit} more")
        lines.append(f"  - {label}: {'; '.join(rendered)}")
    return lines


def format_odysseus_briefing_section(digest: OdysseusTravelDigest) -> str:
    """Render concise Odysseus Travel Brief Markdown with zero em/en dashes."""
    if digest.status in ("none", "unconfigured"):
        return ""
    if digest.status == "error":
        warning = _sanitize_dashes(digest.error or "Odysseus MCP unavailable")
        return "\n".join(["## Travel Brief (Odysseus)", f"- **Warning**: {warning}"])
    if not digest.trips:
        return ""

    lines = ["## Travel Brief (Odysseus)"]
    for brief in digest.trips:
        lines.extend(_format_trip_brief_lines(brief))
    return _sanitize_dashes("\n".join(lines))
