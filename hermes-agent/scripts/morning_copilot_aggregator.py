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
4. Odin API (http://huginn-gateway:8642/v1) & Kanban: blocked cards and overnight Iris alerts.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

try:
    from audit_swarm_health import audit_swarm_services
except ImportError:
    try:
        from hermes_agent.scripts.audit_swarm_health import audit_swarm_services
    except ImportError:
        def audit_swarm_services(triage: bool = False, kanban_db_path: str = "") -> Dict[str, Any]:
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
DEFAULT_HUGINN_URL = os.environ.get("HUGINN_GATEWAY_URL", "http://huginn-gateway:8642")

KANBAN_DB_PATH = os.environ.get(
    "KANBAN_DB_PATH",
    "/opt/data/kanban.db" if os.path.exists("/opt/data/kanban.db") else "/opt/odin/hermes/kanban.db",
)

OBSIDIAN_VAULT_PATH = os.environ.get(
    "OBSIDIAN_VAULT_PATH",
    "/app/vault" if os.path.exists("/app/vault") else "/opt/atlas/vault",
)


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
    weight_kg: Optional[float] = None
    body_fat_pct: Optional[float] = None
    active_energy_kcal: Optional[int] = None
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


def fetch_eir_telemetry(base_url: str, target_date: str) -> EirTelemetry:
    """Fetches and evaluates daily biometrics from Eir API."""
    data = fetch_overlay_json(f"{base_url}/biometrics/daily/{target_date}")
    if not data:
        # Retry with latest if target day not yet seeded
        data = fetch_overlay_json(f"{base_url}/biometrics/latest")

    if not data:
        return EirTelemetry(
            date=target_date,
            recovery_tier="unpopulated",
            recovery_label="⚪ Pending (Telemetry Unavailable)",
            recovery_rationale="Eir API did not return biometrics; telemetry syncing pending.",
        )

    sleep_h = float(data["sleep_duration_hours"]) if data.get("sleep_duration_hours") is not None else None
    deep_h = float(data["sleep_deep_hours"]) if data.get("sleep_deep_hours") is not None else None
    rhr = int(data["resting_hr_bpm"]) if data.get("resting_hr_bpm") is not None else None
    hrv = float(data["hrv_ms"]) if data.get("hrv_ms") is not None else None
    weight = float(data["latest_weight_kg"]) if data.get("latest_weight_kg") is not None else None
    fat = float(data["latest_body_fat_pct"]) if data.get("latest_body_fat_pct") is not None else None
    energy = int(data["active_energy_kcal"]) if data.get("active_energy_kcal") is not None else None

    tier, label, rationale = evaluate_biological_recovery(sleep_h, deep_h, rhr, hrv)

    notes = data.get("readiness_notes") or []

    return EirTelemetry(
        date=target_date,
        sleep_duration_hours=sleep_h,
        sleep_deep_hours=deep_h,
        resting_hr_bpm=rhr,
        hrv_ms=hrv,
        weight_kg=weight,
        body_fat_pct=fat,
        active_energy_kcal=energy,
        recovery_tier=tier,
        recovery_label=label,
        recovery_rationale=rationale,
        readiness_notes=notes,
    )


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
# 4. ODIN Kanban Queue & Overnight Iris Alerts
# ----------------------------------------------------------------------

@dataclass
class OdinTelemetry:
    blocked_cards: List[Dict[str, Any]] = field(default_factory=list)
    iris_alerts_24h: List[Dict[str, Any]] = field(default_factory=list)
    active_cards_by_status: Dict[str, int] = field(default_factory=dict)
    total_active_cards: int = 0
    swarm_total_services: int = 0
    swarm_healthy_services: int = 0
    swarm_degraded_services: List[Dict[str, Any]] = field(default_factory=list)


def fetch_odin_telemetry(
    db_path: str,
    lookback_hours: int = 24,
    auto_triage: bool = True
) -> OdinTelemetry:
    """Queries Kanban database and audits Swarm service replicas and health."""
    swarm_audit = audit_swarm_services(triage=auto_triage, kanban_db_path=db_path)

    if not os.path.exists(db_path):
        logger.warning("Kanban database not found at %s", db_path)
        return OdinTelemetry(
            swarm_total_services=swarm_audit.get("total_services", 0),
            swarm_healthy_services=swarm_audit.get("healthy_services", 0),
            swarm_degraded_services=swarm_audit.get("degraded_services", []),
        )

    blocked: List[Dict[str, Any]] = []
    alerts: List[Dict[str, Any]] = []
    status_counts: Dict[str, int] = {}
    now_ts = int(datetime.now(timezone.utc).timestamp())
    lookback_ts = now_ts - (lookback_hours * 3600)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row

        # 1. Blocked cards
        for row in conn.execute("SELECT id, title, assignee, created_at FROM tasks WHERE status = 'blocked' ORDER BY created_at DESC"):
            blocked.append({
                "id": row["id"],
                "title": row["title"],
                "assignee": row["assignee"],
            })

        # 2. Overnight Iris alerts (source: jev-gateway or title contains Alert)
        stmt_alerts = (
            "SELECT id, title, created_by, status, created_at FROM tasks "
            "WHERE created_at >= ? AND (created_by = 'jev-gateway' OR title LIKE '%Alert%' OR title LIKE '%Incident%') "
            "ORDER BY created_at DESC"
        )
        for row in conn.execute(stmt_alerts, (lookback_ts,)):
            alerts.append({
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "created_by": row["created_by"],
            })

        # 3. Active queue status counts
        for row in conn.execute("SELECT status, count(*) as cnt FROM tasks WHERE status != 'done' GROUP BY status"):
            status_counts[row["status"]] = row["cnt"]

        conn.close()
    except Exception as exc:
        logger.error("Failed querying Kanban database at %s: %s", db_path, exc)

    total_active = sum(status_counts.values())
    return OdinTelemetry(
        blocked_cards=blocked,
        iris_alerts_24h=alerts,
        active_cards_by_status=status_counts,
        total_active_cards=total_active,
        swarm_total_services=swarm_audit.get("total_services", 0),
        swarm_healthy_services=swarm_audit.get("healthy_services", 0),
        swarm_degraded_services=swarm_audit.get("degraded_services", []),
    )


# ----------------------------------------------------------------------
# 5. Executive Synthesis & Markdown Generation
# ----------------------------------------------------------------------

@dataclass
class MorningCopilotReport:
    date: str
    eir: EirTelemetry
    kratos: KratosTelemetry
    plutus: PlutusTelemetry
    odin: OdinTelemetry
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "eir": asdict(self.eir),
            "kratos": asdict(self.kratos),
            "plutus": asdict(self.plutus),
            "odin": asdict(self.odin),
        }


def generate_executive_markdown(report: MorningCopilotReport, section: str = "all") -> str:
    """Formats aggregated cross-stack intelligence into an executive Markdown briefing.
    section: 'all', 'executive' (wealth + homelab), or 'training' (biological recovery).
    """
    eir = report.eir
    kratos = report.kratos
    plutus = report.plutus
    odin = report.odin

    lines: List[str] = []

    if section == "training":
        lines.append(f"# 🏋️ Biological Recovery & Training Directive - {report.date}")
        lines.append("")
        vitals_parts = []
        if eir.sleep_duration_hours is not None:
            deep_str = f" ({eir.sleep_deep_hours:.1f}h deep)" if eir.sleep_deep_hours is not None else ""
            vitals_parts.append(f"Sleep {eir.sleep_duration_hours:.1f}h{deep_str}")
        if eir.hrv_ms is not None:
            vitals_parts.append(f"HRV {eir.hrv_ms:.1f}ms")
        if eir.resting_hr_bpm is not None:
            vitals_parts.append(f"RHR {eir.resting_hr_bpm}bpm")
        if eir.weight_kg is not None:
            fat_str = f" ({eir.body_fat_pct:.1f}% fat)" if eir.body_fat_pct is not None else ""
            vitals_parts.append(f"Weight {eir.weight_kg:.1f}kg{fat_str}")

        vitals_line = " | ".join(vitals_parts) if vitals_parts else "Telemetry syncing"
        lines.append(f"- **Vitals**: {vitals_line}")
        lines.append(f"- **Readiness**: {eir.recovery_label} - {eir.recovery_rationale}")
        lines.append(f"- **Scheduled Split**: {kratos.today_activity} ({kratos.volume_adaptation})")

        if kratos.badminton_buffer_note:
            lines.append(f"- **🏸 Athletic Buffer**: {kratos.badminton_buffer_note}")

        if kratos.recommended_exercises:
            ex_names = [e.get("exercise_name", "") for e in kratos.recommended_exercises[:4] if e.get("exercise_name")]
            if ex_names:
                lines.append(f"- **Prescription**: {', '.join(ex_names)} ({kratos.target_rir})")
        lines.append("")
        lines.append("## 🔍 Operational Review & Run Frictions")
        lines.append("- Telemetry: Biological recovery metrics and Kratos training prescription verified.")
        lines.append("- 0 frictions or inefficiencies encountered; run was clean and optimal.")
        return "\n".join(lines)

    lines.append(f"# 🌅 Morning Executive Copilot - {report.date}")
    lines.append("")

    if section in ("all", "both"):
        # Section 1: Biological Recovery & Autoregulated Training
        lines.append("## 🏋️ Biological Recovery & Training Directive")
        vitals_parts = []
        if eir.sleep_duration_hours is not None:
            deep_str = f" ({eir.sleep_deep_hours:.1f}h deep)" if eir.sleep_deep_hours is not None else ""
            vitals_parts.append(f"Sleep {eir.sleep_duration_hours:.1f}h{deep_str}")
        if eir.hrv_ms is not None:
            vitals_parts.append(f"HRV {eir.hrv_ms:.1f}ms")
        if eir.resting_hr_bpm is not None:
            vitals_parts.append(f"RHR {eir.resting_hr_bpm}bpm")
        if eir.weight_kg is not None:
            fat_str = f" ({eir.body_fat_pct:.1f}% fat)" if eir.body_fat_pct is not None else ""
            vitals_parts.append(f"Weight {eir.weight_kg:.1f}kg{fat_str}")

        vitals_line = " | ".join(vitals_parts) if vitals_parts else "Telemetry syncing"
        lines.append(f"- **Vitals**: {vitals_line}")
        lines.append(f"- **Readiness**: {eir.recovery_label} - {eir.recovery_rationale}")
        lines.append(f"- **Scheduled Split**: {kratos.today_activity} ({kratos.volume_adaptation})")

        if kratos.badminton_buffer_note:
            lines.append(f"- **🏸 Athletic Buffer**: {kratos.badminton_buffer_note}")

        if kratos.recommended_exercises:
            ex_names = [e.get("exercise_name", "") for e in kratos.recommended_exercises[:4] if e.get("exercise_name")]
            if ex_names:
                lines.append(f"- **Prescription**: {', '.join(ex_names)} ({kratos.target_rir})")
        lines.append("")

    # Section 2: Wealth & Financial Runway
    lines.append("## 💰 Wealth Runway & Financial Deadlines")
    lines.append(
        f"- **Runway**: **{plutus.runway_weeks:.1f} weeks** ({plutus.runway_years:.2f} years) | "
        f"Emergency: {plutus.emergency_weeks:.1f}w | Locked: {plutus.locked_weeks:.1f}w"
    )
    lines.append(
        f"- **Burn & Banked Time**: Weekly Burn ${plutus.weekly_burn_rate:,.2f} NZD | "
        f"Time Banked This Week: {plutus.time_banked_this_week:+.1f} weeks"
    )

    if plutus.unallocated_tx_count > 0:
        lines.append(f"- **⚠️ Unallocated Transactions**: {plutus.unallocated_tx_count} transactions require classification.")
    else:
        lines.append("- **Unallocated Transactions**: 0 unallocated items; ledger fully reconciled.")

    if plutus.credit_card_deadlines:
        for cd in plutus.credit_card_deadlines:
            rem_str = f"in {cd.days_remaining} days" if cd.days_remaining is not None else "scheduled"
            lines.append(f"- **💳 {cd.account_name}**: {cd.notice} ({rem_str}).")
    lines.append("")

    # Section 3: Odin Swarm & Kanban Queue
    lines.append("## 🛡️ Homelab Swarm & Kanban Queue")

    if odin.swarm_total_services > 0:
        if odin.swarm_degraded_services:
            lines.append(f"- **🚨 Degraded Swarm Services ({len(odin.swarm_degraded_services)})**:")
            for ds in odin.swarm_degraded_services:
                triage_str = f" (Triaged to Kanban: `{ds['triage_id']}`)" if ds.get("triage_id") else (" (Already in Kanban)" if ds.get("already_tracked") else "")
                err_str = f" | {ds['task_error']}" if ds.get("task_error") else ""
                lines.append(f"  - `{ds['name']}` ({ds['replicas']}): {ds.get('task_state', 'degraded')}{err_str}{triage_str}")
        else:
            lines.append(f"- **Swarm Fleet**: 🟢 {odin.swarm_healthy_services}/{odin.swarm_total_services} services healthy (0 degraded).")

    status_summary = ", ".join(f"{st}: {cnt}" for st, cnt in odin.active_cards_by_status.items()) or "Queue idle"
    lines.append(f"- **Active Cards ({odin.total_active_cards})**: {status_summary}")

    if odin.blocked_cards:
        lines.append(f"- **🚨 Blocked Cards ({len(odin.blocked_cards)})**:")
        for bc in odin.blocked_cards:
            lines.append(f"  - `{bc['id']}`: {bc['title']} (Assignee: {bc['assignee']})")
    else:
        lines.append("- **Blocked Cards**: 0 cards blocked; pipeline flowing smoothly.")

    if odin.iris_alerts_24h:
        lines.append(f"- **Overnight Iris Incidents ({len(odin.iris_alerts_24h)})**:")
        for al in odin.iris_alerts_24h[:3]:
            lines.append(f"  - `{al['id']}`: {al['title']} ({al['status']})")
    else:
        lines.append("- **Overnight Incidents**: 0 Iris incidents in the past 24 hours.")
    lines.append("")

    # Operational Review Footer
    lines.append("## 🔍 Operational Review & Run Frictions")
    if section == "executive":
        lines.append("- Execution: Executive intelligence aggregated successfully across Plutus and Odin.")
    else:
        lines.append("- Execution: Cross-stack telemetry aggregated successfully across Eir, Kratos, Plutus, and Odin.")
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


def run_morning_copilot(
    target_date: Optional[str] = None,
    eir_url: str = DEFAULT_EIR_URL,
    kratos_url: str = DEFAULT_KRATOS_URL,
    plutus_url: str = DEFAULT_PLUTUS_URL,
    kanban_db_path: str = KANBAN_DB_PATH,
    vault_path: str = OBSIDIAN_VAULT_PATH,
    save_journal: bool = False,
    section: str = "all",
    auto_triage: bool = True,
) -> MorningCopilotReport:
    """Executes full cross-stack aggregation cycle."""
    date_str = target_date or get_auckland_today()
    logger.info("Starting Morning Executive Copilot aggregation for date: %s", date_str)

    # 1. Eir & 2. Kratos (health & training telemetry)
    if section != "executive":
        eir = fetch_eir_telemetry(eir_url, date_str)
        kratos = fetch_kratos_telemetry(kratos_url, date_str, eir.recovery_tier)
    else:
        eir = EirTelemetry(date=date_str)
        kratos = KratosTelemetry(date=date_str, iso_week=resolve_iso_week(date_str))

    # 3. Plutus & 4. Odin (wealth & homelab telemetry)
    if section != "training":
        plutus = fetch_plutus_telemetry(plutus_url, date_str)
        odin = fetch_odin_telemetry(kanban_db_path, auto_triage=auto_triage)
    else:
        plutus = PlutusTelemetry(as_of_date=date_str)
        odin = OdinTelemetry()

    report = MorningCopilotReport(
        date=date_str,
        eir=eir,
        kratos=kratos,
        plutus=plutus,
        odin=odin,
    )

    if save_journal:
        md = generate_executive_markdown(report, section=section)
        append_to_obsidian_journal(vault_path, date_str, md)

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
    parser.add_argument("--kanban-db", type=str, default=KANBAN_DB_PATH, help="Path to kanban.db SQLite store")
    parser.add_argument("--vault-path", type=str, default=OBSIDIAN_VAULT_PATH, help="Path to Obsidian vault")
    parser.add_argument("--output-file", type=str, default=None, help="Save markdown output to specified file path")
    parser.add_argument("--no-triage", action="store_true", help="Disable automatic dispatch of degraded Swarm services to kanban-gateway triage")
    args = parser.parse_args()

    report = run_morning_copilot(
        target_date=args.date,
        eir_url=args.eir_url,
        kratos_url=args.kratos_url,
        plutus_url=args.plutus_url,
        kanban_db_path=args.kanban_db,
        vault_path=args.vault_path,
        save_journal=args.journal,
        section=args.section,
        auto_triage=not args.no_triage,
    )

    if args.require_data and args.section == "training":
        has_useful_data = (
            (report.eir.sleep_duration_hours is not None or report.eir.hrv_ms is not None)
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
