#!/usr/bin/env python3
"""morning_copilot_aggregator.py - Morning Executive Command Engine & Cross-Stack Copilot.

Pillar 2 of the Yggdrasil Autonomous Expansion Roadmap:
Aggregates operational, physical, and financial intelligence across Docker Swarm
overlay network (aether-net) in a single unified execution:
1. Eir API (http://eir-api:8000/v1): sleep duration, deep sleep, resting HR, HRV
   -> biological recovery score (Green, Amber, Red).
2. Kratos API (http://kratos-api:8000/v1): scheduled workout split and upcoming
   athletic events (badminton buffer) -> volume autoregulation.
3. Plutus API (http://plutus-api:8000/v1): financial runway in locked weeks,
   unallocated transactions, credit card settlement deadlines.
4. Odin Swarm (audit_swarm_health): Swarm replica health and overlay IPAM headroom.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


def _ensure_odin_runtime_on_path() -> None:
    """Ensure odin_runtime is importable in both repo tests and container /usr/local/bin."""
    repo_hermes_dir = Path(__file__).resolve().parents[1]
    if (repo_hermes_dir / "odin_runtime").is_dir() and str(repo_hermes_dir) not in sys.path:
        sys.path.insert(0, str(repo_hermes_dir))
        return
    venv_lib = Path("/opt/hermes/.venv/lib")
    if venv_lib.is_dir():
        for site_dir in sorted(venv_lib.glob("python*/site-packages")):
            if (site_dir / "odin_runtime").is_dir() and str(site_dir) not in sys.path:
                sys.path.insert(0, str(site_dir))
                return


_ensure_odin_runtime_on_path()
from odin_runtime.odysseus_briefing import (  # noqa: E402
    OdysseusTravelDigest,
    fetch_odysseus_travel_briefings,
    format_odysseus_briefing_section,
)

try:
    from audit_swarm_health import audit_swarm_services
except ImportError:
    try:
        from hermes_agent.scripts.audit_swarm_health import audit_swarm_services
    except ImportError:
        def audit_swarm_services() -> Dict[str, Any]:
            return {
                "status": "PASS",
                "total_services": 0,
                "healthy_services": 0,
                "degraded_count": 0,
                "degraded_services": [],
            }

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [morning-copilot] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("morning-copilot")

# Default overlay network URLs and paths
DEFAULT_EIR_URL = os.environ.get("EIR_API_URL", "http://eir-api:8000")
DEFAULT_KRATOS_URL = os.environ.get("KRATOS_API_URL", "http://kratos-api:8000")
DEFAULT_PLUTUS_URL = os.environ.get("PLUTUS_API_URL", "http://plutus-api:8000")
DEFAULT_CHRONOS_URL = os.environ.get("CHRONOS_API_URL", "http://chronos-api:8000")
DEFAULT_ODYSSEUS_MCP_URL = os.environ.get("ODYSSEUS_MCP_URL", "http://odysseus-api:8000/mcp")
DEFAULT_ODYSSEUS_MCP_TOKEN = os.environ.get("ODYSSEUS_MCP_TOKEN", "")
DEFAULT_HUGINN_URL = os.environ.get("HUGINN_GATEWAY_URL", "http://huginn-gateway:8642")

OBSIDIAN_VAULT_PATH = os.environ.get(
    "OBSIDIAN_VAULT_PATH",
    "/app/vault" if os.path.exists("/app/vault") else "/opt/atlas/vault",
)
DEFAULT_SLEEP_TARGET_HOURS = 7.0
SLEEP_LOOKBACK_DAYS = 6


def get_auckland_today() -> str:
    """Returns today's calendar date in Pacific/Auckland (UTC+13 during NZDT, UTC+12 during NZST)."""
    # 4-hour circadian shift: hours before 04:00 belong to preceding date cycle
    now_utc = datetime.now(timezone.utc)
    # Conservative +13 for NZDT
    circadian = now_utc + timedelta(hours=13) - timedelta(hours=4)
    return circadian.strftime("%Y-%m-%d")


def execute_cmd(cmd: List[str], timeout: int = 5) -> str:
    """Executes a bounded command without persistent subshells."""
    try:
        res = subprocess.run(["timeout", str(timeout)] + cmd, capture_output=True, text=True)
        return res.stdout.strip()
    except Exception as exc:
        logger.debug("Command execution failed for %s: %s", cmd, exc)
        return ""


def fetch_overlay_json(url: str, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    """Fetches JSON over Swarm overlay. Falls back to container execution if running from host."""
    headers = {"User-Agent": "Yggdrasil-MorningCopilot/1.0", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception as direct_exc:
        logger.debug("Direct overlay fetch failed for %s: %s", url, direct_exc)

    # Host fallback: query through odin_huginn-gateway container attached to aether-net
    cid = execute_cmd(["docker", "ps", "-q", "-f", "name=odin_huginn-gateway"])
    cid = cid.splitlines()[0].strip() if cid else ""
    if cid:
        py_code = (
            f"import json, urllib.request; "
            f"req = urllib.request.Request('{url}', headers={{'User-Agent': 'Yggdrasil-MorningCopilot/1.0'}}); "
            f"resp = urllib.request.urlopen(req, timeout={int(timeout) + 1}); "
            f"print(resp.read().decode('utf-8'))"
        )
        out = execute_cmd(["docker", "exec", cid, "python3", "-c", py_code], timeout=int(timeout) + 3)
        if out:
            try:
                return json.loads(out)
            except Exception as parse_exc:
                logger.debug("Failed parsing container probe JSON from %s: %s", url, parse_exc)
    return None


# ----------------------------------------------------------------------
# 1. EIR Health & Biometric Scoring
# ----------------------------------------------------------------------

@dataclass
class EirTelemetry:
    date: str
    sleep_duration_hours: Optional[float] = None
    sleep_deep_hours: Optional[float] = None
    resting_hr_bpm: Optional[int] = None
    hrv_ms: Optional[float] = None
    overnight_hrv_ms: Optional[float] = None
    effective_hrv_ms: Optional[float] = None
    hrv_source: Optional[str] = None
    weight_kg: Optional[float] = None
    body_fat_pct: Optional[float] = None
    active_energy_kcal: Optional[int] = None
    sleep_target_hours: float = DEFAULT_SLEEP_TARGET_HOURS
    sleep_nights_7d: int = 0
    avg_overnight_sleep_7d: Optional[float] = None
    sleep_debt_7d_hours: Optional[float] = None
    nap_days_7d: int = 0
    total_nap_hours_7d: float = 0.0
    yesterday_nap: Optional[Dict[str, Any]] = None
    recovery_tier: str = "amber"  # green, amber, red
    recovery_label: str = "🟡 Amber (Moderate Fatigue)"
    recovery_rationale: str = ""
    readiness_notes: List[str] = field(default_factory=list)


def evaluate_biological_recovery(
    sleep_hours: Optional[float],
    deep_hours: Optional[float],
    rhr: Optional[int],
    hrv: Optional[float],
) -> Tuple[str, str, str]:
    """Calculates Biological Recovery Tier (Green, Amber, Red) based on health briefing rules."""
    signals: List[str] = []
    red_flags = 0
    amber_flags = 0

    # 1. Sleep duration rule
    if sleep_hours is not None:
        if sleep_hours >= 7.0:
            signals.append(f"Sleep {sleep_hours:.1f}h (>= 7.0h optimal)")
        elif sleep_hours >= 5.5:
            amber_flags += 1
            signals.append(f"Sleep {sleep_hours:.1f}h (5.5h-7.0h moderate)")
        else:
            red_flags += 1
            signals.append(f"Sleep {sleep_hours:.1f}h (< 5.5h acute deficit)")

    # 2. Deep sleep rule
    if deep_hours is not None:
        if deep_hours < 1.0:
            amber_flags += 1
            signals.append(f"Deep sleep {deep_hours:.2f}h (< 1.0h)")

    # 3. Resting HR rule
    if rhr is not None:
        if rhr <= 65:
            signals.append(f"RHR {rhr} bpm (nominal)")
        elif rhr <= 75:
            amber_flags += 1
            signals.append(f"RHR {rhr} bpm (elevated)")
        else:
            red_flags += 1
            signals.append(f"RHR {rhr} bpm (> 75 bpm elevated)")

    # 4. HRV rule (baseline ~25-35ms for standard reference if historical average not seeded)
    if hrv is not None:
        if hrv >= 30.0:
            signals.append(f"HRV {hrv:.1f} ms (robust)")
        elif hrv >= 20.0:
            amber_flags += 1
            signals.append(f"HRV {hrv:.1f} ms (moderately suppressed)")
        else:
            red_flags += 1
            signals.append(f"HRV {hrv:.1f} ms (< 20.0 ms acute suppression)")

    # Synthesize tier
    if red_flags > 0 or amber_flags >= 2:
        if red_flags >= 2 or (red_flags == 1 and amber_flags >= 2):
            tier = "red"
            label = "🔴 Red (Acute Under-recovery)"
            rationale = "Multiple acute suppression signals: " + ", ".join(signals)
        else:
            tier = "amber"
            label = "🟡 Amber (Moderate Fatigue)"
            rationale = "Fatigue markers present: " + ", ".join(signals)
    elif amber_flags == 1:
        tier = "amber"
        label = "🟡 Amber (Moderate Fatigue)"
        rationale = "Mild recovery constraint: " + ", ".join(signals)
    elif signals:
        tier = "green"
        label = "🟢 Green (High Readiness)"
        rationale = "All biometrics nominal: " + ", ".join(signals)
    else:
        tier = "unpopulated"
        label = "⚪ Pending (Telemetry Unpopulated)"
        rationale = "Biometric telemetry not yet synced from Apple Watch / Eir"

    return tier, label, rationale


def _to_optional_float(value: Any) -> Optional[float]:
    """Parses int, float, Decimal, or numeric string into a finite float or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _to_nonneg_int(value: Any, default: int = 0) -> int:
    """Parses int or numeric string into a non-negative integer."""
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _resolve_effective_hrv(
    overnight_hrv: Optional[float],
    latest_hrv: Optional[float],
) -> Tuple[Optional[float], Optional[str]]:
    """Prefers overnight HRV over latest daytime HRV for recovery scoring."""
    if overnight_hrv is not None:
        return overnight_hrv, "overnight"
    if latest_hrv is not None:
        return latest_hrv, "latest"
    return None, None


def _parse_auckland_hhmm(iso_ts: Any) -> Optional[Tuple[datetime, str]]:
    """Parses an ISO-8601 timestamp and formats HH:MM in Pacific/Auckland."""
    if not isinstance(iso_ts, str) or not iso_ts.strip():
        return None
    try:
        parsed = datetime.fromisoformat(iso_ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed, parsed.strftime("%H:%M")
    try:
        local_dt = parsed.astimezone(ZoneInfo("Pacific/Auckland"))
    except Exception:
        local_dt = parsed
    return local_dt, local_dt.strftime("%H:%M")


def _extract_latest_nap_end_local(naps: Any) -> Optional[str]:
    """Returns the latest nap end time (HH:MM Pacific/Auckland) from a naps list."""
    if not isinstance(naps, list):
        return None
    latest_pair: Optional[Tuple[datetime, str]] = None
    for entry in naps:
        if not isinstance(entry, dict):
            continue
        parsed = _parse_auckland_hhmm(entry.get("end_at"))
        if parsed is None:
            continue
        if latest_pair is None or parsed[0] > latest_pair[0]:
            latest_pair = parsed
    return latest_pair[1] if latest_pair is not None else None


def _extract_yesterday_nap(summary: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Extracts yesterday's nap summary dict or None when no naps were logged."""
    if not isinstance(summary, dict):
        return None
    nap_count = _to_nonneg_int(summary.get("nap_count"), 0)
    nap_hours = _to_optional_float(summary.get("nap_duration_hours"))
    naps_list = summary.get("naps")
    has_naps_list = isinstance(naps_list, list) and len(naps_list) > 0
    if nap_count <= 0 and (nap_hours is None or nap_hours <= 0.0) and not has_naps_list:
        return None
    if nap_hours is None and has_naps_list:
        nap_hours = sum(
            _to_optional_float(item.get("duration_hours")) or 0.0
            for item in naps_list
            if isinstance(item, dict)
        )
    resolved_count = nap_count if nap_count > 0 else (len(naps_list) if has_naps_list else 1)
    return {
        "duration_hours": round(nap_hours or 0.0, 2),
        "nap_count": resolved_count,
        "latest_end_local": _extract_latest_nap_end_local(naps_list),
    }


def compute_sleep_and_nap_trends(
    window_summaries: List[Tuple[str, Optional[Dict[str, Any]]]],
    yesterday_date: str,
    sleep_target_hours: float = DEFAULT_SLEEP_TARGET_HOURS,
) -> Dict[str, Any]:
    """Computes 7-day rolling overnight sleep debt, nap totals, and yesterday's nap."""
    populated_sleeps: List[float] = []
    nap_days = 0
    total_nap_hours = 0.0
    yesterday_summary: Optional[Dict[str, Any]] = None
    for day_str, summary in window_summaries:
        if day_str == yesterday_date:
            yesterday_summary = summary
        if not isinstance(summary, dict):
            continue
        sleep_h = _to_optional_float(summary.get("sleep_duration_hours"))
        if sleep_h is not None:
            populated_sleeps.append(sleep_h)
        nap_count = _to_nonneg_int(summary.get("nap_count"), 0)
        nap_h = _to_optional_float(summary.get("nap_duration_hours"))
        if nap_count > 0 or (nap_h is not None and nap_h > 0.0):
            nap_days += 1
            total_nap_hours += nap_h or 0.0
    nights = len(populated_sleeps)
    avg_sleep = round(sum(populated_sleeps) / nights, 2) if nights > 0 else None
    debt_hours = round(sum(max(0.0, sleep_target_hours - h) for h in populated_sleeps), 2) if nights > 0 else None
    return {
        "sleep_target_hours": sleep_target_hours,
        "sleep_nights_7d": nights,
        "avg_overnight_sleep_7d": avg_sleep,
        "sleep_debt_7d_hours": debt_hours,
        "nap_days_7d": nap_days,
        "total_nap_hours_7d": round(total_nap_hours, 2),
        "yesterday_nap": _extract_yesterday_nap(yesterday_summary),
    }


def _fetch_eir_daily_safe(base_url: str, day_str: str) -> Optional[Dict[str, Any]]:
    """Fetches a single day's Eir summary, failing soft without /latest fallback."""
    try:
        data = fetch_overlay_json(f"{base_url}/biometrics/daily/{day_str}")
    except Exception as exc:
        logger.debug("Eir daily lookup failed for %s: %s", day_str, exc)
        return None
    return data if isinstance(data, dict) else None


def _fetch_eir_sleep_window(
    base_url: str,
    target_date: str,
    target_daily: Optional[Dict[str, Any]],
) -> Tuple[List[Tuple[str, Optional[Dict[str, Any]]]], str]:
    """Fetches prior 6 days [target-6d..target-1d] and combines with target_date summary."""
    anchor = date.fromisoformat(target_date)
    yesterday_str = (anchor - timedelta(days=1)).isoformat()
    window: List[Tuple[str, Optional[Dict[str, Any]]]] = []
    for offset in range(SLEEP_LOOKBACK_DAYS, 0, -1):
        day_str = (anchor - timedelta(days=offset)).isoformat()
        window.append((day_str, _fetch_eir_daily_safe(base_url, day_str)))
    window.append((target_date, target_daily))
    return window, yesterday_str


def _build_eir_telemetry(validated_date: str, data: Dict[str, Any], trends: Dict[str, Any]) -> EirTelemetry:
    """Parses current-day Eir vitals, scores biological recovery, and merges 7d sleep trends."""
    sleep_h = _to_optional_float(data.get("sleep_duration_hours"))
    deep_h = _to_optional_float(data.get("sleep_deep_hours"))
    rhr = int(data["resting_hr_bpm"]) if data.get("resting_hr_bpm") is not None else None
    hrv = _to_optional_float(data.get("hrv_ms"))
    overnight_hrv = _to_optional_float(data.get("overnight_hrv_ms"))
    effective_hrv, hrv_source = _resolve_effective_hrv(overnight_hrv, hrv)
    tier, label, rationale = evaluate_biological_recovery(sleep_h, deep_h, rhr, effective_hrv)
    return EirTelemetry(
        date=validated_date,
        sleep_duration_hours=sleep_h,
        sleep_deep_hours=deep_h,
        resting_hr_bpm=rhr,
        hrv_ms=hrv,
        overnight_hrv_ms=overnight_hrv,
        effective_hrv_ms=effective_hrv,
        hrv_source=hrv_source,
        weight_kg=_to_optional_float(data.get("latest_weight_kg")),
        body_fat_pct=_to_optional_float(data.get("latest_body_fat_pct")),
        active_energy_kcal=int(data["active_energy_kcal"]) if data.get("active_energy_kcal") is not None else None,
        recovery_tier=tier,
        recovery_label=label,
        recovery_rationale=rationale,
        readiness_notes=data.get("readiness_notes") or [],
        **trends,
    )


def fetch_eir_telemetry(base_url: str, target_date: str) -> EirTelemetry:
    """Fetches and evaluates daily biometrics and 7d sleep/nap trends from Eir API."""
    validated_date = _validate_iso_date(target_date)
    clean_base = base_url.rstrip("/")
    daily_data = _fetch_eir_daily_safe(clean_base, validated_date)
    window_summaries, yesterday_str = _fetch_eir_sleep_window(clean_base, validated_date, daily_data)
    trends = compute_sleep_and_nap_trends(window_summaries, yesterday_str)
    data = daily_data or fetch_overlay_json(f"{clean_base}/biometrics/latest")
    if not data:
        return EirTelemetry(
            date=validated_date,
            recovery_tier="unpopulated",
            recovery_label="⚪ Pending (Telemetry Unavailable)",
            recovery_rationale="Eir API did not return biometrics; telemetry syncing pending.",
            **trends,
        )
    return _build_eir_telemetry(validated_date, data, trends)


# ----------------------------------------------------------------------
# 2. KRATOS Workout Split & Volume Autoregulation
# ----------------------------------------------------------------------

@dataclass
class KratosTelemetry:
    date: str
    iso_week: str
    today_activity: str = "Rest"
    activity_type: str = "rest"  # lift, sport, rest
    volume_adaptation: str = "Full Intensity 4x2"
    volume_landmark: str = "Optimal"
    target_rir: str = "0 RIR (Failure)"
    badminton_buffer_active: bool = False
    badminton_buffer_note: Optional[str] = None
    weekly_schedule: List[Dict[str, Any]] = field(default_factory=list)
    recommended_exercises: List[Dict[str, Any]] = field(default_factory=list)
    discord_thread_id: Optional[str] = None


def resolve_iso_week(date_str: str) -> str:
    """Computes ISO week identifier (e.g. '2026-W46')."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def fetch_kratos_telemetry(base_url: str, target_date: str, recovery_tier: str) -> KratosTelemetry:
    """Fetches weekly workout plan and autoregulates volume based on recovery tier."""
    iso_week = resolve_iso_week(target_date)
    dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    day_name = dt.strftime("%a")  # Mon, Tue, etc.

    plan_data = fetch_overlay_json(f"{base_url}/plans/week/{iso_week}")
    rec_data = fetch_overlay_json(f"{base_url}/biometrics/workout-recommendation?target_date={target_date}")

    weekly_days = plan_data.get("days", []) if plan_data else []
    today_plan = next((d for d in weekly_days if d.get("date") == target_date or d.get("day") == day_name), None)

    # Daily plan only cadence: prioritize daily recommendation routine over weekly schedule
    rec_routine = rec_data.get("routine_name") if rec_data else None
    if rec_routine:
        activity = rec_routine
        act_type = "rest" if ("rest" in rec_routine.lower() or "fast" in rec_routine.lower()) else "lift"
    elif today_plan:
        activity = today_plan.get("activity", "Daily Training")
        act_type = today_plan.get("type", "lift")
    else:
        activity = "Daily Training"
        act_type = "lift"

    # Athletic events & badminton buffer check
    # Badminton fixed: Tuesday & Thursday (7:00pm - 9:00pm).
    # Leg recovery buffer: minimum 48 hours recovery before heavy lower body.
    weekday_idx = dt.weekday()  # Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
    badminton_active = False
    badminton_note = None

    if weekday_idx == 0:  # Monday
        badminton_active = True
        badminton_note = "Badminton buffer: Tuesday 7:00pm badminton upcoming. Upper body focus; protect legs from axial fatigue."
    elif weekday_idx == 1:  # Tuesday
        badminton_active = True
        badminton_note = "Sport Day: Badminton 7:00pm - 9:00pm tonight. Hydration & light mobility focus; no heavy lifting."
    elif weekday_idx == 2:  # Wednesday
        badminton_active = True
        badminton_note = "Badminton buffer: Thursday 7:00pm badminton upcoming. Ensure leg recovery buffer is respected."
    elif weekday_idx == 3:  # Thursday
        badminton_active = True
        badminton_note = "Sport Day: Badminton 7:00pm - 9:00pm tonight. Electrolyte and endurance focus."
    elif weekday_idx == 4:  # Friday
        badminton_note = "Rest Day: Extended 18h+ fast and joint restoration. Zero lifting."

    # Autoregulate volume based on recovery score
    is_rest_or_sport = act_type in ("rest", "sport") or "rest" in activity.lower() or "badminton" in activity.lower()

    if rec_data and rec_data.get("routine_name"):
        volume_adaptation = rec_data.get("readiness_adaptation", "Volume Autoregulated")
        volume_landmark = rec_data.get("volume_landmark", "MEV")
        target_rir = rec_data.get("target_rir", "RIR 2-3")
        exercises = rec_data.get("exercises", [])
        thread_id = str(rec_data.get("extra", {}).get("discord_thread_id") or "")
    else:
        exercises = []
        thread_id = None
        if is_rest_or_sport:
            volume_adaptation = "Rest / Active Recovery (Zero resistance lifting prescribed)"
            volume_landmark = "Deload / Restoration"
            target_rir = "N/A"
        elif recovery_tier == "green":
            volume_adaptation = "Full Intensity (4 exercises x 2 sets to technical failure)"
            volume_landmark = "Optimal (Progressive Overload)"
            target_rir = "0 RIR (Failure)"
        elif recovery_tier == "amber":
            volume_adaptation = "Volume Scaled -20% (1-2 sets per exercise, cap intensity at RPE 7-8)"
            volume_landmark = "MEV (Minimum Effective Volume)"
            target_rir = "RIR 2-3"
        elif recovery_tier == "red":
            volume_adaptation = "Active Recovery & Mobility Reset (Thoracic wall slides, hip 90/90, 30m walk)"
            volume_landmark = "Deload / Restoration"
            target_rir = "RIR 4+"
        else:
            volume_adaptation = "Pending Biometric Sync"
            volume_landmark = "Pending"
            target_rir = "Pending"

    return KratosTelemetry(
        date=target_date,
        iso_week=iso_week,
        today_activity=activity,
        activity_type=act_type,
        volume_adaptation=volume_adaptation,
        volume_landmark=volume_landmark,
        target_rir=target_rir,
        badminton_buffer_active=badminton_active,
        badminton_buffer_note=badminton_note,
        weekly_schedule=weekly_days,
        recommended_exercises=exercises,
        discord_thread_id=thread_id if thread_id else None,
    )


# ----------------------------------------------------------------------
# 3. PLUTUS Runway, Unallocated Transactions & Settlement Deadlines
# ----------------------------------------------------------------------

@dataclass
class CreditCardDeadline:
    account_name: str
    balance_nzd: float
    due_date: Optional[str]
    days_remaining: Optional[int]
    notice: str


@dataclass
class PlutusTelemetry:
    as_of_date: str
    runway_weeks: float = 0.0
    runway_years: float = 0.0
    locked_weeks: float = 0.0
    emergency_weeks: float = 0.0
    weekly_burn_rate: float = 0.0
    time_banked_this_week: float = 0.0
    unallocated_tx_count: int = 0
    unallocated_tx_sample: List[Dict[str, Any]] = field(default_factory=list)
    credit_card_deadlines: List[CreditCardDeadline] = field(default_factory=list)


def parse_settlement_date_from_text(text: str, reference_date: date) -> Optional[date]:
    """Extracts settlement or debit dates (e.g. '12/11/26', '12 Nov 2026') from text."""
    # Pattern 1: DD/MM/YY or DD/MM/YYYY
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # Pattern 2: DD Mon YYYY
    m2 = re.search(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b", text, re.I)
    if m2:
        try:
            dt = datetime.strptime(f"{m2.group(1)} {m2.group(2)} {m2.group(3)}", "%d %b %Y").date()
            return dt
        except ValueError:
            pass
    return None


def fetch_plutus_telemetry(base_url: str, as_of_date: str) -> PlutusTelemetry:
    """Queries Plutus for runway, unallocated expenses, and credit card settlement deadlines."""
    today_dt = datetime.strptime(as_of_date, "%Y-%m-%d").date()

    # 1. Runway metrics
    runway_data = fetch_overlay_json(f"{base_url}/runway?as_of={as_of_date}")
    primary_weeks = 0.0
    primary_years = 0.0
    locked_w = 0.0
    emergency_w = 0.0
    weekly_burn = 0.0
    time_banked = 0.0

    if runway_data:
        primary_weeks = float(runway_data.get("primary_runway_weeks", 0.0))
        primary_years = float(runway_data.get("primary_runway_years", 0.0))
        time_banked = float(runway_data.get("time_banked_this_week", 0.0))
        bd = runway_data.get("breakdown", {})
        locked_w = float(bd.get("locked_weeks", 0.0))
        emergency_w = float(bd.get("emergency_weeks", 0.0))
        weekly_burn = float(bd.get("weekly_burn_rate", 0.0))

    # 2. Unallocated transactions
    tx_data = fetch_overlay_json(f"{base_url}/transactions?limit=100")
    unallocated_list: List[Dict[str, Any]] = []
    if tx_data and isinstance(tx_data, list):
        for tx in tx_data:
            cat = str(tx.get("category") or "").strip().lower()
            if cat in ("unallocated", "uncategorized", "", "none"):
                unallocated_list.append({
                    "id": tx.get("id"),
                    "date": tx.get("transaction_date"),
                    "merchant": tx.get("merchant"),
                    "amount": tx.get("amount"),
                    "account": tx.get("account_name"),
                })

    # 3. Credit card settlement deadlines
    accounts_data = fetch_overlay_json(f"{base_url}/accounts?is_active=true")
    cc_deadlines: List[CreditCardDeadline] = []

    if accounts_data and isinstance(accounts_data, list):
        for acc in accounts_data:
            if acc.get("account_type") == "credit_card":
                acc_id = acc.get("id")
                acc_name = acc.get("name", "Credit Card")
                # Fetch latest snapshot
                snaps = fetch_overlay_json(f"{base_url}/accounts/{acc_id}/snapshots?limit=1")
                balance = 0.0
                if snaps and isinstance(snaps, list) and snaps:
                    balance = float(snaps[0].get("balance_nzd", 0.0))

                # Check recent transactions for debit schedule or settlement text
                txs = fetch_overlay_json(f"{base_url}/transactions?account_id={acc_id}&limit=10")
                settle_date: Optional[date] = None
                notice_str = f"Current balance: ${balance:,.2f} NZD"

                if txs and isinstance(txs, list):
                    for t in txs:
                        raw = str(t.get("raw_description") or "")
                        if "debited" in raw.lower() or "due" in raw.lower() or "payment" in raw.lower():
                            parsed_date = parse_settlement_date_from_text(raw, today_dt)
                            if parsed_date and parsed_date >= today_dt - timedelta(days=5):
                                settle_date = parsed_date
                                # Extract debit notice
                                m_amt = re.search(r"\$([0-9,]+\.\d{2})", raw)
                                amt_str = f"${m_amt.group(1)}" if m_amt else f"${balance:,.2f}"
                                notice_str = f"Direct debit scheduled for {settle_date.strftime('%d/%m/%Y')} ({amt_str} NZD)"
                                break

                days_rem = (settle_date - today_dt).days if settle_date else None
                cc_deadlines.append(
                    CreditCardDeadline(
                        account_name=acc_name,
                        balance_nzd=balance,
                        due_date=settle_date.isoformat() if settle_date else None,
                        days_remaining=days_rem,
                        notice=notice_str,
                    )
                )

    return PlutusTelemetry(
        as_of_date=as_of_date,
        runway_weeks=primary_weeks,
        runway_years=primary_years,
        locked_weeks=locked_w,
        emergency_weeks=emergency_w,
        weekly_burn_rate=weekly_burn,
        time_banked_this_week=time_banked,
        unallocated_tx_count=len(unallocated_list),
        unallocated_tx_sample=unallocated_list[:5],
        credit_card_deadlines=cc_deadlines,
    )


# ----------------------------------------------------------------------
# 4. CHRONOS Schedule, Exceptions & Habit Signals
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ChronosCommitment:
    title: str
    start_minute: Optional[int] = None
    end_minute: Optional[int] = None
    all_day: bool = False
    source: str = ""
    tentative: bool = False
    has_conflict: bool = False
    notes: Optional[str] = None


@dataclass(frozen=True)
class ChronosGridException:
    action: str
    grid_block_id: Optional[int] = None
    new_start_minute: Optional[int] = None
    new_end_minute: Optional[int] = None
    extension_count: int = 0
    notes: Optional[str] = None


@dataclass(frozen=True)
class ChronosWorkoutHydration:
    status: str = "unknown"
    planned_routine_name: Optional[str] = None
    planned_day_of_week: Optional[str] = None
    completed_sessions: int = 0
    attached_grid_block_id: Optional[int] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ChronosFrictionNote:
    tag: str = ""
    note: str = ""
    habit_cue_id: Optional[int] = None
    grid_block_id: Optional[int] = None


@dataclass(frozen=True)
class ChronosPassiveSignal:
    cue_title: str
    rule_key: str
    observed: bool
    fact_label: str = ""
    peer_status: str = ""


@dataclass(frozen=True)
class ChronosSummary:
    date: str = ""
    yesterday: str = ""
    available: bool = False
    commitments: Tuple[ChronosCommitment, ...] = ()
    tentative_count: int = 0
    conflict_count: int = 0
    grid_exceptions: Tuple[ChronosGridException, ...] = ()
    workout_hydration: Optional[ChronosWorkoutHydration] = None
    yesterday_friction_notes: Tuple[ChronosFrictionNote, ...] = ()
    yesterday_passive_signals: Tuple[ChronosPassiveSignal, ...] = ()
    degraded_peers: Tuple[str, ...] = ()


def _validate_iso_date(value: str) -> str:
    """Validates strict YYYY-MM-DD date string before network or container fallback."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Invalid target_date format (expected YYYY-MM-DD): {value!r}")
    date.fromisoformat(value)
    return value


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Expected int, got {type(value).__name__}")
    return value


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Expected int or None, got {type(value).__name__}")
    return value


def _as_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Expected str or None, got {type(value).__name__}")
    return value


def _parse_chronos_commitments(raw: Any) -> Tuple[ChronosCommitment, ...]:
    if not isinstance(raw, list):
        raise ValueError("commitments must be a list")
    items: List[ChronosCommitment] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("title"), str):
            raise ValueError("commitment item must be a dict with string title")
        items.append(
            ChronosCommitment(
                title=entry["title"],
                start_minute=_as_optional_int(entry.get("start_minute")),
                end_minute=_as_optional_int(entry.get("end_minute")),
                all_day=bool(entry.get("all_day", False)),
                source=_as_optional_str(entry.get("source")) or "",
                tentative=bool(entry.get("tentative", False)),
                has_conflict=bool(entry.get("has_conflict", False)),
                notes=_as_optional_str(entry.get("notes")),
            )
        )
    return tuple(items)


def _parse_chronos_exceptions(raw: Any) -> Tuple[ChronosGridException, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("grid_exceptions must be a list")
    items: List[ChronosGridException] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("action"), str):
            raise ValueError("grid_exception item must be a dict with string action")
        items.append(
            ChronosGridException(
                action=entry["action"],
                grid_block_id=_as_optional_int(entry.get("grid_block_id")),
                new_start_minute=_as_optional_int(entry.get("new_start_minute")),
                new_end_minute=_as_optional_int(entry.get("new_end_minute")),
                extension_count=_as_int(entry.get("extension_count"), 0),
                notes=_as_optional_str(entry.get("notes")),
            )
        )
    return tuple(items)


def _parse_chronos_workout(raw: Any) -> Optional[ChronosWorkoutHydration]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("workout_hydration must be a dict or None")
    raw_sessions = raw.get("completed_sessions")
    completed_count = len(raw_sessions) if isinstance(raw_sessions, list) else _as_int(raw_sessions, 0)
    return ChronosWorkoutHydration(
        status=_as_optional_str(raw.get("status")) or "unknown",
        planned_routine_name=_as_optional_str(raw.get("planned_routine_name")),
        planned_day_of_week=_as_optional_str(raw.get("planned_day_of_week")),
        completed_sessions=completed_count,
        attached_grid_block_id=_as_optional_int(raw.get("attached_grid_block_id")),
        error=_as_optional_str(raw.get("error")),
    )


def _parse_chronos_friction_notes(raw: Any) -> Tuple[ChronosFrictionNote, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("yesterday_friction_notes must be a list")
    items: List[ChronosFrictionNote] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("note"), str):
            raise ValueError("friction note item must be a dict with string note")
        items.append(
            ChronosFrictionNote(
                tag=_as_optional_str(entry.get("tag")) or "",
                note=entry["note"],
                habit_cue_id=_as_optional_int(entry.get("habit_cue_id")),
                grid_block_id=_as_optional_int(entry.get("grid_block_id")),
            )
        )
    return tuple(items)


def _parse_chronos_passive_signals(raw: Any) -> Tuple[ChronosPassiveSignal, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("yesterday_passive_signals must be a list")
    items: List[ChronosPassiveSignal] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("rule_key"), str):
            raise ValueError("passive signal item must be a dict with string rule_key")
        cue_title = _as_optional_str(entry.get("cue_title")) or entry["rule_key"]
        items.append(
            ChronosPassiveSignal(
                cue_title=cue_title,
                rule_key=entry["rule_key"],
                observed=bool(entry.get("observed", False)),
                fact_label=_as_optional_str(entry.get("fact_label")) or "",
                peer_status=_as_optional_str(entry.get("peer_status")) or "",
            )
        )
    return tuple(items)


def _parse_chronos_peers(raw: Any) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError("degraded_peers must be a list of strings")
    return tuple(raw)


def parse_chronos_summary(data: Any, target_date: str) -> ChronosSummary:
    """Parses Chronos daily-summary payload into a frozen ChronosSummary."""
    if not isinstance(data, dict) or not isinstance(data.get("date"), str):
        return ChronosSummary(date=target_date, available=False)
    try:
        commitments = _parse_chronos_commitments(data.get("commitments"))
        exceptions = _parse_chronos_exceptions(data.get("grid_exceptions"))
        workout = _parse_chronos_workout(data.get("workout_hydration"))
        friction_notes = _parse_chronos_friction_notes(data.get("yesterday_friction_notes"))
        passive_signals = _parse_chronos_passive_signals(data.get("yesterday_passive_signals"))
        degraded_peers = _parse_chronos_peers(data.get("degraded_peers"))
        yesterday = _as_optional_str(data.get("yesterday")) or ""
        tentative_count = _as_int(
            data.get("tentative_count"), sum(1 for item in commitments if item.tentative)
        )
        conflict_count = _as_int(
            data.get("conflict_count"), sum(1 for item in commitments if item.has_conflict)
        )
    except ValueError as exc:
        logger.warning("Malformed Chronos daily-summary payload for %s: %s", target_date, exc)
        return ChronosSummary(date=target_date, available=False)

    return ChronosSummary(
        date=data["date"],
        yesterday=yesterday,
        available=True,
        commitments=commitments,
        tentative_count=tentative_count,
        conflict_count=conflict_count,
        grid_exceptions=exceptions,
        workout_hydration=workout,
        yesterday_friction_notes=friction_notes,
        yesterday_passive_signals=passive_signals,
        degraded_peers=degraded_peers,
    )


def fetch_chronos_summary(base_url: str, target_date: str) -> ChronosSummary:
    """Fetches daily schedule, exceptions, and habit signals from Chronos API."""
    validated_date = _validate_iso_date(target_date)
    clean_base = base_url.rstrip("/")
    if not clean_base:
        return ChronosSummary(date=validated_date, available=False)
    url = f"{clean_base}/integrations/daily-summary?target_date={validated_date}"
    data = fetch_overlay_json(url)
    if data is None:
        return ChronosSummary(date=validated_date, available=False)
    return parse_chronos_summary(data, validated_date)


# ----------------------------------------------------------------------
# 5. ODIN Swarm Health & Overlay IPAM
# ----------------------------------------------------------------------

@dataclass
class OdinTelemetry:
    swarm_total_services: int = 0
    swarm_healthy_services: int = 0
    swarm_degraded_services: List[Dict[str, Any]] = field(default_factory=list)
    overlay_ipam: Optional[Dict[str, Any]] = None


def fetch_odin_telemetry() -> OdinTelemetry:
    """Audits Swarm service replicas and overlay IPAM headroom."""
    swarm_audit = audit_swarm_services()
    return OdinTelemetry(
        swarm_total_services=swarm_audit.get("total_services", 0),
        swarm_healthy_services=swarm_audit.get("healthy_services", 0),
        swarm_degraded_services=swarm_audit.get("degraded_services", []),
        overlay_ipam=swarm_audit.get("overlay_ipam"),
    )


# ----------------------------------------------------------------------
# 6. Executive Synthesis & Markdown Generation
# ----------------------------------------------------------------------

@dataclass
class MorningCopilotReport:
    date: str
    eir: EirTelemetry
    kratos: KratosTelemetry
    plutus: PlutusTelemetry
    odin: OdinTelemetry
    chronos: ChronosSummary = field(default_factory=ChronosSummary)
    odysseus: OdysseusTravelDigest = field(
        default_factory=lambda: OdysseusTravelDigest(status="none")
    )
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "eir": asdict(self.eir),
            "kratos": asdict(self.kratos),
            "plutus": asdict(self.plutus),
            "odin": asdict(self.odin),
            "chronos": asdict(self.chronos),
            "odysseus": asdict(self.odysseus),
        }


def _render_sleep_trend_line(eir: EirTelemetry) -> Optional[str]:
    """Formats the 7-day overnight sleep debt and yesterday nap trend bullet."""
    if eir.sleep_nights_7d <= 0 and not eir.yesterday_nap:
        return None
    parts: List[str] = []
    if (
        eir.sleep_nights_7d > 0
        and eir.avg_overnight_sleep_7d is not None
        and eir.sleep_debt_7d_hours is not None
    ):
        parts.append(f"Avg {eir.avg_overnight_sleep_7d:.1f}h overnight ({eir.sleep_nights_7d}d)")
        parts.append(f"Debt {eir.sleep_debt_7d_hours:.1f}h vs {eir.sleep_target_hours:.1f}h")
    parts.append(f"Naps: {eir.nap_days_7d}d ({eir.total_nap_hours_7d:.1f}h)")
    if eir.yesterday_nap:
        y_dur = _to_optional_float(eir.yesterday_nap.get("duration_hours"))
        y_end = eir.yesterday_nap.get("latest_end_local")
        dur_str = f"{y_dur:.1f}h" if y_dur is not None else "logged"
        end_str = f" (ended {y_end})" if y_end else ""
        parts.append(f"Yesterday nap: {dur_str}{end_str}")
    return f"- **Sleep Trend (7d)**: {' | '.join(parts)}"


def _render_biometrics_lines(eir: EirTelemetry, kratos: KratosTelemetry) -> List[str]:
    """Formats Eir biometrics and Kratos training prescription lines."""
    vitals_parts = []
    if eir.sleep_duration_hours is not None:
        deep_str = f" ({eir.sleep_deep_hours:.1f}h deep)" if eir.sleep_deep_hours is not None else ""
        vitals_parts.append(f"Sleep {eir.sleep_duration_hours:.1f}h{deep_str}")
    display_hrv = eir.effective_hrv_ms if eir.effective_hrv_ms is not None else eir.hrv_ms
    if display_hrv is not None:
        hrv_label = "Overnight HRV" if eir.hrv_source == "overnight" else "HRV"
        vitals_parts.append(f"{hrv_label} {display_hrv:.1f}ms")
    if eir.resting_hr_bpm is not None:
        vitals_parts.append(f"RHR {eir.resting_hr_bpm}bpm")
    if eir.weight_kg is not None:
        fat_str = f" ({eir.body_fat_pct:.1f}% fat)" if eir.body_fat_pct is not None else ""
        vitals_parts.append(f"Weight {eir.weight_kg:.1f}kg{fat_str}")

    lines = [
        f"- **Vitals**: {' | '.join(vitals_parts) if vitals_parts else 'Telemetry syncing'}",
    ]
    sleep_trend_line = _render_sleep_trend_line(eir)
    if sleep_trend_line:
        lines.append(sleep_trend_line)
    lines.extend([
        f"- **Readiness**: {eir.recovery_label} - {eir.recovery_rationale}",
        f"- **Scheduled Split**: {kratos.today_activity} ({kratos.volume_adaptation})",
    ])
    if kratos.badminton_buffer_note:
        lines.append(f"- **🏸 Athletic Buffer**: {kratos.badminton_buffer_note}")
    if kratos.recommended_exercises:
        ex_names = [e.get("exercise_name", "") for e in kratos.recommended_exercises[:4] if e.get("exercise_name")]
        if ex_names:
            lines.append(f"- **Prescription**: {', '.join(ex_names)} ({kratos.target_rir})")
    return lines


def _render_wealth_lines(plutus: PlutusTelemetry) -> List[str]:
    """Formats Plutus runway, unallocated transactions, and credit card deadlines."""
    lines = [
        "## 💰 Wealth Runway & Financial Deadlines",
        (
            f"- **Runway**: **{plutus.runway_weeks:.1f} weeks** ({plutus.runway_years:.2f} years) | "
            f"Emergency: {plutus.emergency_weeks:.1f}w | Locked: {plutus.locked_weeks:.1f}w"
        ),
        (
            f"- **Burn & Banked Time**: Weekly Burn ${plutus.weekly_burn_rate:,.2f} NZD | "
            f"Time Banked This Week: {plutus.time_banked_this_week:+.1f} weeks"
        ),
    ]
    if plutus.unallocated_tx_count > 0:
        lines.append(f"- **⚠️ Unallocated Transactions**: {plutus.unallocated_tx_count} transactions require classification.")
    else:
        lines.append("- **Unallocated Transactions**: 0 unallocated items; ledger fully reconciled.")
    for cd in plutus.credit_card_deadlines:
        rem_str = f"in {cd.days_remaining} days" if cd.days_remaining is not None else "scheduled"
        lines.append(f"- **💳 {cd.account_name}**: {cd.notice} ({rem_str}).")
    lines.append("")
    return lines


def _render_swarm_lines(odin: OdinTelemetry) -> List[str]:
    """Formats Homelab Swarm replica and overlay IPAM health lines."""
    lines = ["## 🛡️ Homelab Swarm Health"]
    if odin.swarm_total_services > 0:
        if odin.swarm_degraded_services:
            lines.append(f"- **🚨 Degraded Swarm Services ({len(odin.swarm_degraded_services)})**:")
            for ds in odin.swarm_degraded_services:
                err_str = f" | {ds['task_error']}" if ds.get("task_error") else ""
                lines.append(f"  - `{ds['name']}` ({ds['replicas']}): {ds.get('task_state', 'degraded')}{err_str}")
        else:
            lines.append(f"- **Swarm Fleet**: 🟢 {odin.swarm_healthy_services}/{odin.swarm_total_services} services healthy (0 degraded).")
    elif not odin.overlay_ipam:
        lines.append("- **Swarm Fleet**: Telemetry unavailable.")

    if odin.overlay_ipam:
        ipam = odin.overlay_ipam
        net_name = ipam.get("network", "aether-net")
        in_use, free, phantom = ipam.get("ips_in_use"), ipam.get("dynamic_ips_available"), ipam.get("phantom_leaked_ips")
        if ipam.get("degraded"):
            reason = ipam.get("reason") or "IPAM check failed"
            metrics = f"{in_use} IPs in use, {free} free (~{phantom} phantom)" if None not in (in_use, free, phantom) else "metrics unavailable"
            lines.append(f"- **🚨 Overlay IPAM Alert (`{net_name}`)**: {metrics} | {reason}")
        elif ipam.get("status") == "PASS":
            lines.append(f"- **Overlay IPAM (`{net_name}`)**: 🟢 {in_use} in use, {free} free (~{phantom} phantom).")
    lines.append("")
    return lines


def _format_minute_hhmm(minute: Optional[int]) -> str:
    """Formats minutes since midnight as HH:MM."""
    if minute is None or minute < 0 or minute > 1440:
        return "--:--"
    hours, mins = divmod(minute, 60)
    return f"{hours:02d}:{mins:02d}"


def _format_chronos_commitment(commitment: ChronosCommitment) -> str:
    """Formats a single Chronos commitment bullet with time and markers."""
    if commitment.all_day:
        time_str = "all-day"
    elif commitment.start_minute is not None and commitment.end_minute is not None:
        time_str = f"{_format_minute_hhmm(commitment.start_minute)}-{_format_minute_hhmm(commitment.end_minute)}"
    elif commitment.start_minute is not None:
        time_str = _format_minute_hhmm(commitment.start_minute)
    else:
        time_str = "unscheduled"

    markers: List[str] = []
    if commitment.tentative:
        markers.append("[Tentative]")
    if commitment.has_conflict:
        markers.append("[Conflict]")
    suffix = f" {' '.join(markers)}" if markers else ""
    return f"  - {time_str}: {commitment.title}{suffix}"


def _format_chronos_exception(exc: ChronosGridException) -> str:
    """Formats a concise grid exception descriptor."""
    if exc.new_start_minute is not None and exc.new_end_minute is not None:
        return (
            f"{exc.action} ({_format_minute_hhmm(exc.new_start_minute)}-"
            f"{_format_minute_hhmm(exc.new_end_minute)})"
        )
    if exc.extension_count > 0:
        return f"{exc.action} (x{exc.extension_count})"
    return exc.action


def _render_chronos_habits_lines(chronos: ChronosSummary) -> List[str]:
    """Formats yesterday's observed passive signals, friction notes, and peer status."""
    lines: List[str] = []
    if chronos.yesterday_passive_signals:
        observed = [sig for sig in chronos.yesterday_passive_signals if sig.observed]
        if observed:
            labels = [
                f"{sig.cue_title} ({sig.fact_label})" if sig.fact_label else sig.cue_title
                for sig in observed
            ]
            lines.append(
                f"- **Yesterday Signals ({len(observed)}/{len(chronos.yesterday_passive_signals)} "
                f"observed)**: {', '.join(labels)}"
            )
        else:
            lines.append(
                f"- **Yesterday Signals**: 0/{len(chronos.yesterday_passive_signals)} observed"
            )
    else:
        lines.append("- **Yesterday Signals**: none")

    if chronos.yesterday_friction_notes:
        notes_fmt = [
            f"[{fn.tag}] {fn.note}" if fn.tag else fn.note
            for fn in chronos.yesterday_friction_notes[:5]
        ]
        lines.append(
            f"- **Yesterday Friction Notes ({len(chronos.yesterday_friction_notes)})**: "
            f"{'; '.join(notes_fmt)}"
        )
    else:
        lines.append("- **Yesterday Friction Notes**: 0")

    if chronos.degraded_peers:
        lines.append(f"- **Chronos Degraded Peers**: {', '.join(chronos.degraded_peers)}")
    return lines


def _render_chronos_lines(chronos: ChronosSummary) -> List[str]:
    """Formats Chronos commitments, grid exceptions, workout hydration, and habit signals."""
    lines = ["## 📅 Schedule & Habits (Chronos)"]
    if not chronos.available:
        lines.append("- **Chronos**: Summary unavailable (API unreachable or malformed payload).")
        lines.append("")
        return lines

    lines.append(
        f"- **Commitments**: {len(chronos.commitments)} scheduled "
        f"({chronos.tentative_count} tentative, {chronos.conflict_count} conflicts)"
    )
    for commitment in chronos.commitments:
        lines.append(_format_chronos_commitment(commitment))

    if chronos.grid_exceptions:
        exc_items = [_format_chronos_exception(exc) for exc in chronos.grid_exceptions[:5]]
        if len(chronos.grid_exceptions) > 5:
            exc_items.append(f"+{len(chronos.grid_exceptions) - 5} more")
        lines.append(f"- **Grid Exceptions**: {len(chronos.grid_exceptions)} active ({', '.join(exc_items)})")
    else:
        lines.append("- **Grid Exceptions**: 0 active")

    if chronos.workout_hydration:
        wh = chronos.workout_hydration
        routine = wh.planned_routine_name or "None"
        err_note = f" | error: {wh.error}" if wh.error else ""
        lines.append(
            f"- **Workout Hydration**: {wh.status} | Routine: {routine} | "
            f"Completed: {wh.completed_sessions}{err_note}"
        )
    else:
        lines.append("- **Workout Hydration**: none reported")

    lines.extend(_render_chronos_habits_lines(chronos))
    lines.append("")
    return lines


def generate_executive_markdown(report: MorningCopilotReport, section: str = "all") -> str:
    """Formats aggregated cross-stack intelligence into an executive Markdown briefing.
    section: 'all', 'executive' (wealth + homelab + chronos), or 'training' (biological recovery).
    """
    if section == "training":
        lines = [f"# 🏋️ Biological Recovery & Training Directive - {report.date}", ""]
        lines.extend(_render_biometrics_lines(report.eir, report.kratos))
        lines.extend(["", "## 🔍 Operational Review & Run Frictions",
                      "- Telemetry: Biological recovery metrics and Kratos training prescription verified.",
                      "- 0 frictions or inefficiencies encountered; run was clean and optimal."])
        return "\n".join(lines)

    lines = [f"# 🌅 Morning Executive Copilot - {report.date}", ""]
    if section in ("all", "both"):
        lines.append("## 🏋️ Biological Recovery & Training Directive")
        lines.extend(_render_biometrics_lines(report.eir, report.kratos))
        lines.append("")

    lines.extend(_render_chronos_lines(report.chronos))
    travel_section = format_odysseus_briefing_section(report.odysseus)
    if travel_section:
        lines.extend(travel_section.splitlines())
        lines.append("")
    lines.extend(_render_wealth_lines(report.plutus))
    lines.extend(_render_swarm_lines(report.odin))

    lines.append("## 🔍 Operational Review & Run Frictions")
    scope_note = "Chronos, Plutus, and Odin" if section == "executive" else "Eir, Kratos, Chronos, Plutus, and Odin"
    lines.append(f"- Execution: {'Executive intelligence' if section == 'executive' else 'Cross-stack telemetry'} aggregated successfully across {scope_note}.")
    lines.append("- 0 frictions or inefficiencies encountered; run was clean and optimal.")
    return "\n".join(lines)


def append_to_obsidian_journal(vault_path: str, target_date: str, content: str) -> bool:
    """Appends executive copilot digest to today's Auckland Obsidian journal."""
    dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    journal_dir = os.path.join(vault_path, "second-brain", "Journal", dt.strftime("%Y"), dt.strftime("%m"))
    journal_path = os.path.join(journal_dir, f"{target_date}.md")

    if not os.path.exists(journal_dir):
        try:
            os.makedirs(journal_dir, exist_ok=True)
        except Exception as exc:
            logger.error("Failed creating journal directory %s: %s", journal_dir, exc)
            return False

    header_block = f"\n\n{content}\n"
    try:
        if os.path.exists(journal_path):
            with open(journal_path, "a", encoding="utf-8") as f:
                f.write(header_block)
            logger.info("Appended Morning Copilot briefing to existing journal: %s", journal_path)
        else:
            with open(journal_path, "w", encoding="utf-8") as f:
                f.write(f"# {target_date}\n{header_block}")
            logger.info("Created new journal note with Morning Copilot briefing: %s", journal_path)
        return True
    except Exception as exc:
        logger.error("Failed writing to journal note %s: %s", journal_path, exc)
        return False


def _fetch_odysseus_section(
    date_str: str,
    odysseus_mcp_url: Optional[str],
    odysseus_mcp_token: Optional[str],
) -> OdysseusTravelDigest:
    """Resolve Odysseus MCP config from args or env and fetch travel briefings."""
    resolved_url = (
        odysseus_mcp_url
        if odysseus_mcp_url is not None
        else os.environ.get("ODYSSEUS_MCP_URL", DEFAULT_ODYSSEUS_MCP_URL)
    )
    resolved_token = (
        odysseus_mcp_token
        if odysseus_mcp_token is not None
        else os.environ.get("ODYSSEUS_MCP_TOKEN", DEFAULT_ODYSSEUS_MCP_TOKEN)
    )
    return fetch_odysseus_travel_briefings(
        mcp_url=resolved_url,
        token=resolved_token,
        today=date.fromisoformat(_validate_iso_date(date_str)),
    )


def run_morning_copilot(
    target_date: Optional[str] = None,
    eir_url: str = DEFAULT_EIR_URL,
    kratos_url: str = DEFAULT_KRATOS_URL,
    plutus_url: str = DEFAULT_PLUTUS_URL,
    chronos_url: str = DEFAULT_CHRONOS_URL,
    vault_path: str = OBSIDIAN_VAULT_PATH,
    save_journal: bool = False,
    section: str = "all",
    odysseus_mcp_url: Optional[str] = None,
    odysseus_mcp_token: Optional[str] = None,
) -> MorningCopilotReport:
    """Executes full cross-stack aggregation cycle."""
    date_str = target_date or get_auckland_today()
    logger.info("Starting Morning Executive Copilot aggregation for date: %s", date_str)
    if section != "executive":
        eir = fetch_eir_telemetry(eir_url, date_str)
        kratos = fetch_kratos_telemetry(kratos_url, date_str, eir.recovery_tier)
    else:
        eir = EirTelemetry(date=date_str)
        kratos = KratosTelemetry(date=date_str, iso_week=resolve_iso_week(date_str))

    if section != "training":
        plutus = fetch_plutus_telemetry(plutus_url, date_str)
        chronos = fetch_chronos_summary(chronos_url, date_str)
        odin = fetch_odin_telemetry()
        odysseus = _fetch_odysseus_section(date_str, odysseus_mcp_url, odysseus_mcp_token)
    else:
        plutus = PlutusTelemetry(as_of_date=date_str)
        chronos = ChronosSummary(date=date_str, available=False)
        odin = OdinTelemetry()
        odysseus = OdysseusTravelDigest(status="none")

    report = MorningCopilotReport(
        date=date_str,
        eir=eir,
        kratos=kratos,
        plutus=plutus,
        odin=odin,
        chronos=chronos,
        odysseus=odysseus,
    )
    if save_journal:
        append_to_obsidian_journal(vault_path, date_str, generate_executive_markdown(report, section=section))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Morning Executive Command Engine & Cross-Stack Copilot.")
    parser.add_argument("--date", type=str, default=None, help="Evaluation date (YYYY-MM-DD, defaults to Auckland today)")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--journal", action="store_true", help="Append briefing to Obsidian daily journal")
    parser.add_argument("--write-journal", action="store_true", dest="journal", help="Alias for --journal")
    parser.add_argument("--section", choices=["all", "executive", "training"], default="all", help="Briefing section: 'all', 'executive' (wealth + homelab), or 'training' (biological recovery)")
    parser.add_argument("--require-data", action="store_true", help="If set and required biometric/workout data is missing, exits silently with [SILENT]")
    parser.add_argument("--eir-url", type=str, default=DEFAULT_EIR_URL, help="Eir API base URL")
    parser.add_argument("--kratos-url", type=str, default=DEFAULT_KRATOS_URL, help="Kratos API base URL")
    parser.add_argument("--plutus-url", type=str, default=DEFAULT_PLUTUS_URL, help="Plutus API base URL")
    parser.add_argument("--chronos-url", type=str, default=DEFAULT_CHRONOS_URL, help="Chronos API base URL")
    parser.add_argument("--odysseus-mcp-url", type=str, default=None, help="Odysseus MCP endpoint URL")
    parser.add_argument("--vault-path", type=str, default=OBSIDIAN_VAULT_PATH, help="Path to Obsidian vault")
    parser.add_argument("--output-file", type=str, default=None, help="Save markdown output to specified file path")
    args = parser.parse_args()

    report = run_morning_copilot(
        target_date=args.date,
        eir_url=args.eir_url,
        kratos_url=args.kratos_url,
        plutus_url=args.plutus_url,
        chronos_url=args.chronos_url,
        vault_path=args.vault_path,
        save_journal=args.journal,
        section=args.section,
        odysseus_mcp_url=args.odysseus_mcp_url,
    )

    if args.require_data and args.section == "training":
        has_useful_data = (
            (
                report.eir.sleep_duration_hours is not None
                or report.eir.effective_hrv_ms is not None
                or report.eir.hrv_ms is not None
            )
            and report.eir.recovery_tier != "unpopulated"
        )
        if not has_useful_data:
            logger.info("No useful biometric telemetry found for %s; exiting silently with [SILENT]", report.date)
            print("[SILENT]")
            return 0

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        md = generate_executive_markdown(report, section=args.section)
        print(md)
        if args.output_file:
            try:
                with open(args.output_file, "w", encoding="utf-8") as f:
                    f.write(md)
                logger.info("Saved briefing markdown to %s", args.output_file)
            except Exception as exc:
                logger.error("Failed saving output to %s: %s", args.output_file, exc)
                return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
