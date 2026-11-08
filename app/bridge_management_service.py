"""Bounded, sanitized observations; no lifecycle, journal or provider mutations."""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.config import configured_linked_device_lines, quarantined_linked_device_line_ids
from app.bridge_management_repository import bridge_management_repository
from app.whatsapp_client import linked_device_client
from app.whatsapp_line_health import summarize_storage


REASONS = {
    "temporary_transport": "Connection interrupted. Automatic reconnection may recover this session.",
    "restart_required": "WhatsApp requested a connection restart.",
    "logged_out": "WhatsApp has logged this session out. Super Admin pairing review is required.",
    "authentication_failure": "WhatsApp rejected the saved authentication. Super Admin review is required.",
    "session_conflict": "Another session replaced this connection. Session ownership needs review.",
    "protocol_failure": "WhatsApp could not accept this connection. Technical review is required.",
    "storage_failure": "The saved session requires authentication or storage review.",
}
AUTH_BLOCKERS = set("""FINAL_EVIDENCE_MISSING OWNER_NOT_RETIRED TRANSPORT_RELEASE_UNCONFIRMED
EVENT_SUBSCRIPTION_NOT_RELEASED WORK_NOT_SETTLED EVIDENCE_DURABILITY_UNCONFIRMED
OPERATION_ACCOUNTING_INCOMPLETE AUTH_MUTATIONS_REQUIRE_RECONCILIATION OFFLINE_JOURNAL_VALIDATION_REQUIRED
JOURNAL_STATE_MISMATCH FINAL_CREDENTIALS_MISMATCH OWNER_EVIDENCE_MISMATCH INTENT_EVIDENCE_MISMATCH
OWNERSHIP_TRANSITION_IN_PROGRESS EVIDENCE_OBSERVATION_TIMEOUT LIVE_OWNER_OR_WORK_UNCONFIRMED
LINE_WORK_NOT_SETTLED LINE_PERSISTENCE_UNCONFIRMED DIRECTORY_OWNERSHIP_UNPROVEN
FINAL_EVIDENCE_INVALID AUTH_EVIDENCE_MISSING STATE_INTEGRITY_UNPROVEN""".split())
NETWORK_CODES = set("""ECONNRESET ECONNREFUSED EPIPE ETIMEDOUT ENETUNREACH EHOSTUNREACH EAI_AGAIN
ENOTFOUND EACCES EADDRNOTAVAIL ERR_TLS_CERT_ALTNAME_INVALID CERT_HAS_EXPIRED
DEPTH_ZERO_SELF_SIGNED_CERT UNABLE_TO_VERIFY_LEAF_SIGNATURE ERR_SSL_WRONG_VERSION_NUMBER OTHER""".split())
AUDIT_ACTIONS = {
    "LINKED_DEVICE_LINE_CREATED": "Linked-device registration created",
    "LINKED_DEVICE_REMOVAL_STARTED": "Number removal started",
    "LINKED_DEVICE_LINE_REMOVED": "Number removed from the application",
    "WHATSAPP_NUMBER_RENAMED": "Number display name changed",
    "WHATSAPP_NUMBER_ENABLED": "Number enabled in the application",
    "WHATSAPP_NUMBER_DISABLED": "Number disabled in the application",
    "LINKED_DEVICE_HISTORY_RESTORED": "Historical conversations restored",
}


def mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def number(value: Any) -> int | float | None:
    return value if type(value) in {int, float} and 0 <= value <= 10**15 else None


def choice(value, allowed, default=None):
    return value if isinstance(value, str) and value in allowed else default


def iso(value: datetime | float | None = None) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def label(value: Any, limit: int = 80) -> str | None:
    return ''.join(c for c in value if c.isprintable())[:limit] if isinstance(value, str) else None


def phone(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"\+?[0-9 ()-]{5,32}", value) else None


def reason_codes(value: Any) -> list[str]:
    return sorted({choice(code, AUTH_BLOCKERS, "UNRECOGNIZED_EVIDENCE_BLOCKER")
                   for code in value[:50]}) if isinstance(value, list) else []


def sanitize_assessment(value: Any) -> dict:
    value = mapping(value)
    reference = value.get("evidence_reference")
    operations = value.get("operations") if isinstance(value.get("operations"), list) else []
    return {
        "status": choice(value.get("status"), {"BLOCKED", "OPERATOR_REVIEW_REQUIRED"}, "BLOCKED"),
        "authentication_evidence_complete": value.get("authentication_evidence_complete") is True,
        "recovery_allowed": False,
        "blockers": reason_codes(value.get("blockers")),
        "evidence_reference": reference if isinstance(reference, str) and re.fullmatch(r"[a-f0-9]{64}", reference) else None,
        "sequence": number(value.get("sequence")),
        "connection_generation": number(value.get("connection_generation")),
        "operation_count": len(operations), "operations_truncated": len(operations) > 100,
        "operations": [{"source": choice(mapping(op).get("source"), {
            "get", "set", "transaction", "pipeline", "messageMutex", "notificationMutex", "receiptMutex",
            "appStatePatchMutex", "bridgeTask", "bridgeDetachedTask"}, "unknown"),
            **{key: choice(mapping(op).get(key), {"completed", "rejected", "unresolved", "not_started"}, "unresolved")
               for key in ("provider_outcome", "wrapper_outcome")}}
            for op in operations[:100]],
        "notice": "Observation only. Offline journal validation, exclusive ownership and a separately reviewed recovery procedure are required. No fence was changed.",
    }


def describe_line(line_id: str, row: dict, raw: dict | None, storage: Any,
                  quarantined: bool, observed_at: str, now: float) -> dict:
    raw, row = mapping(raw), mapping(row)
    diagnostics = mapping(raw.get("diagnostics"))
    transport = mapping(diagnostics.get("transport"))
    lifecycle = choice(raw.get("lifecycle"), {"active", "gracefully_retired", "fenced_unresolved", "fenced_retained", "decommissioned"}, "unknown")
    if quarantined:
        lifecycle = "decommissioned"
    quarantined = quarantined or lifecycle == "decommissioned"
    state = choice(raw.get("status"), {"connected", "connecting", "disconnected", "qr", "logged_out", "error", "decommissioned"}, "unknown")
    category = choice(raw.get("recovery_category"), REASONS)
    isolation = mapping(raw.get("isolation"))
    fenced = lifecycle.startswith("fenced_")
    auth_state = "abandoned" if quarantined else "review_required" if fenced or category == "authentication_failure" else "logged_out" if state == "logged_out" else "paired" if raw.get("is_paired") is True else "pairing" if state == "qr" else "unknown"
    retry = raw.get("retry_scheduled") if type(raw.get("retry_scheduled")) is bool else None
    remote = mapping(raw.get("local_removal")).get("unlinked")
    remote = remote if type(remote) is bool else None
    blockers = reason_codes(isolation.get("blockers"))
    if fenced:
        blockers.append("AUTHENTICATION_REVIEW_REQUIRED")
    if quarantined:
        blockers.append("IDENTITY_DECOMMISSIONED")
    if not raw:
        blockers.append("TELEMETRY_UNAVAILABLE")
    if category in {"logged_out", "authentication_failure", "session_conflict", "protocol_failure"}:
        blockers.append({"logged_out": "SESSION_LOGGED_OUT", "authentication_failure": "AUTHENTICATION_REJECTED",
                         "session_conflict": "SESSION_OWNERSHIP_CONFLICT", "protocol_failure": "PROTOCOL_REVIEW_REQUIRED"}[category])
    if state == "qr":
        blockers.append("PAIRING_REQUIRED")
    if state == "connecting":
        blockers.append("TRANSPORT_TRANSITION_IN_PROGRESS")
    if row.get("is_enabled") is not None and not row.get("is_enabled"):
        blockers.append("APP_ROUTING_DISABLED")
    if quarantined:
        explanation = ("This number was removed locally. WhatsApp-side unlinking is confirmed." if remote is True else
                       "This number was removed locally, but WhatsApp-side unlinking has not been confirmed.")
    elif fenced:
        explanation = "The saved session requires authentication review. Reconnect is unavailable because the saved session is not safe to reuse."
    elif retry is True:
        explanation = "Connection interrupted. Automatic reconnection is in progress."
    else:
        explanation = REASONS.get(category, "Connected to WhatsApp." if state == "connected" else "WhatsApp is temporarily unavailable." if raw else "Connection telemetry is unavailable. The current state cannot be verified.")
    scoped_storage = summarize_storage(storage, [{"phone_number_id": line_id}])
    # A missing/invalid issue list cannot establish healthy storage. In
    # particular, do not turn a future/unknown critical code into green health.
    storage_payload = mapping(storage)
    storage_issues = storage_payload.get("issues")
    complete_storage = isinstance(storage_issues, list) and all(
        summarize_storage({"level": "warning", "issues": [issue]},
                          [{"phone_number_id": str(mapping(issue).get("line_id") or line_id)}])["issues"]
        for issue in storage_issues
    )
    if (not complete_storage or
            (choice(storage_payload.get("level"), {"warning", "critical"}) and not storage_issues)):
        scoped_storage = {"level": "unknown", "issues": (scoped_storage or {}).get("issues", [])}
        if storage_payload:
            blockers.append("STORAGE_TELEMETRY_INCOMPLETE")
    # Missing/unsupported monitoring is unknown, never healthy by omission.
    storage_state = scoped_storage or {"level": "unknown", "issues": []}
    storage_state["checked_at_ms"] = number(mapping(storage).get("checked_at"))
    if any(issue["level"] == "critical" for issue in storage_state["issues"]):
        blockers.append("STORAGE_REVIEW_REQUIRED")
    try:
        if not isinstance(transport.get("remote_ip"), str) or "%" in transport["remote_ip"]:
            raise ValueError("INVALID_ENDPOINT")
        address = str(ipaddress.ip_address(transport.get("remote_ip")))
        family = "IPv4" if ipaddress.ip_address(address).version == 4 else "IPv6"
    except (ValueError, TypeError):
        address, family = None, "unknown"
    since = number(raw.get("logged_out_at") if state == "logged_out" else raw.get("disconnected_since"))
    actions = []
    for key, title in (("reconnect", "Safe reconnect"), ("pause", "Pause / Resume"), ("relink", "Relink with QR"),
                       ("decommission", "Permanent decommission"), ("auth_recovery", "Authentication assessment"),
                       ("delivery_reconciliation", "Delivery reconciliation")):
        actions.append({"action": key, "label": title, "enabled": False,
                        "reason_code": "PHASE_A_READ_ONLY", "blockers": list(dict.fromkeys(blockers)),
                        "reason": "Recovery controls are not enabled in this read-only release."})
    return {"line_id": line_id, "display_name": label(row.get("display_name")) or "Identity " + line_id,
            "phone": phone(row.get("display_phone_number")) or phone(raw.get("display_phone_number")),
            "is_enabled": False if quarantined else bool(row["is_enabled"]) if row.get("is_enabled") is not None else None,
            "lifecycle": lifecycle, "observed_at": observed_at if raw else None,
            "telemetry_available": bool(raw), "explanation": explanation, "blockers": list(dict.fromkeys(blockers)),
            "connection": {"state": state, "reason_code": category,
                "last_disconnect_reason": choice(diagnostics.get("last_category"), REASONS),
                "disconnected_since_ms": since,
                "outage_seconds": max(0, int(now - since / 1000)) if since is not None and state != "connected" else None,
                "retry_scheduled": retry, "reconnect_attempts": number(diagnostics.get("reconnect_attempts")),
                "last_outage_seconds": number(diagnostics.get("recovery_seconds_last")),
                "disconnects_total": number(diagnostics.get("disconnects_total"))},
            "authentication": {"state": auth_state, "recovery_allowed": False,
                "isolation_safe": isolation.get("safe") if type(isolation.get("safe")) is bool else None,
                "remote_unlinked": remote},
            "storage": storage_state,
            "transport": {"remote_ip": address, "family": family,
                "remote_port": number(transport.get("remote_port")),
                "configured_ip_family": diagnostics.get("configured_ip_family") if type(diagnostics.get("configured_ip_family")) is int and diagnostics["configured_ip_family"] in {0, 4, 6} else None,
                "tls_authorized": transport.get("tls_authorized") if type(transport.get("tls_authorized")) is bool else None,
                "error_code": choice(diagnostics.get("transport_error_code"), NETWORK_CODES)},
            "heartbeat": {"supported": diagnostics.get("supported") is True,
                "age_seconds": max(0, int(now - diagnostics["heartbeat_observed_at"] / 1000)) if number(diagnostics.get("heartbeat_observed_at")) else None,
                **{key: number(diagnostics.get(key)) for key in (
                "heartbeat_success_at", "heartbeat_request_at", "heartbeat_observed_at", "heartbeat_rtt_ms",
                "heartbeat_failures", "heartbeat_timeouts", "heartbeat_query_timeouts")}},
            "delivery": {key: number(raw.get(key)) for key in (
                "pending_callbacks", "oldest_callback_age_seconds", "failed_callbacks", "waiting_history_reactions", "uncertain_sends")},
            "actions": actions}


class BridgeManagementService:
    def __init__(self, repository=None, client=None):
        self.repository = repository or bridge_management_repository
        self.client = client or linked_device_client
        self.lock = asyncio.Lock()
        self.snapshot = None
        self.at = -100.0
        self.metadata_at = -100.0
        self.rows = []
        self.labels = {}
        self.incidents = []
        self.started_at = iso()
        self.history_cache = {}
        self.assessment_cache = {}
        self.detail_lock = asyncio.Lock()

    async def management(self) -> dict:
        async with self.lock:
            if self.snapshot is not None and time.monotonic() - self.at < 5:
                return copy.deepcopy(self.snapshot)
            issues = []
            try:
                active, quarantined = configured_linked_device_lines(), quarantined_linked_device_line_ids()
            except ValueError:
                active, quarantined = {}, set()
                issues.append("CONFIGURATION_UNAVAILABLE")
            refresh_metadata = time.monotonic() - self.metadata_at >= 30
            if refresh_metadata:
                try:
                    self.rows = await asyncio.wait_for(self.repository.numbers(), 3)
                    self.metadata_ok = True
                except Exception:
                    self.metadata_ok = False
                self.metadata_at = time.monotonic()
            missing_labels = sorted(line_id for line_id in quarantined if not self.labels.get(line_id))[:32] if refresh_metadata else []
            if missing_labels:
                labels = await asyncio.gather(*(asyncio.wait_for(self.repository.retired_label(line_id), 1)
                                                for line_id in missing_labels), return_exceptions=True)
                for line_id, result in zip(missing_labels, labels):
                    self.labels[line_id] = result if isinstance(result, dict) else {}
            if not getattr(self, "metadata_ok", False):
                issues.append("REGISTRY_UNAVAILABLE")
            by_id = {str(row["phone_number_id"]): row for row in self.rows
                     if re.fullmatch(r"[0-9]{5,32}", str(row.get("phone_number_id", "")))}
            ids = set(active) | quarantined | set(by_id)
            try:
                payload = await asyncio.wait_for(self.client.get_management_status(), 5)
                bridge_ok = True
            except Exception as exc:
                payload, bridge_ok = {}, False
                issues.append(choice(getattr(exc, "bridge_code", None),
                    {"BRIDGE_INITIALIZING", "STOPPING", "BRIDGE_TRANSPORT_UNKNOWN"}, "BRIDGE_UNAVAILABLE"))
            now, checked_at = time.time(), iso()
            raw_lines = {item["line_id"]: item for item in payload.get("lines", [])
                         if isinstance(item, dict) and isinstance(item.get("line_id"), str) and item["line_id"] in ids}
            previous = {line["line_id"]: line for line in (self.snapshot or {}).get("lines", [])}
            lines = []
            for line_id in sorted(ids):
                row = by_id.get(line_id, {**self.labels.get(line_id, {}), "is_enabled": False if line_id in quarantined else None})
                row = {**row, "display_phone_number": row.get("display_phone_number") or active.get(line_id)}
                line = describe_line(line_id, row, raw_lines.get(line_id), payload.get("storage"), line_id in quarantined, checked_at, now)
                if not line["telemetry_available"] and line_id in previous:
                    line["last_observed_at"] = previous[line_id].get("observed_at") or previous[line_id].get("last_observed_at")
                signature = lambda item: (item["connection"]["state"], item["connection"]["reason_code"], item["lifecycle"], item["storage"]["level"])
                if ((line_id in previous and signature(line) != signature(previous[line_id])) or
                        (line_id not in previous and line["connection"]["state"] not in {"connected", "unknown"})):
                    self.incidents.insert(0, {"line_id": line_id, "observed_at": checked_at,
                        "code": line["connection"]["reason_code"] or line["connection"]["state"], "message": line["explanation"]})
                lines.append(line)
            self.incidents = [event for event in self.incidents if event["line_id"] in ids][:100]
            self.history_cache = {key: value for key, value in self.history_cache.items() if key in ids}
            self.assessment_cache = {key: value for key, value in self.assessment_cache.items() if key in ids}
            runtime = mapping(payload.get("runtime"))
            self.snapshot = {"checked_at": checked_at, "refresh_seconds": 5, "bridge_available": bridge_ok,
                "runtime": {"phase": choice(runtime.get("phase"), {"ready", "starting", "storage_replay", "connecting", "storage_degraded", "stopping"}, "unknown"),
                            "ready": runtime.get("ready") if type(runtime.get("ready")) is bool else None},
                "issues": issues, "lines": lines, "recent_incidents": list(self.incidents[:30]),
                "incident_window_started_at": self.started_at,
                "incident_notice": "Observed transitions in this API worker only; restarts and gaps may omit incidents. Saved operator history is available per number."}
            self.at = time.monotonic()
            # Never cache raw bridge snapshots, QR payloads or auth evidence.
            return copy.deepcopy(self.snapshot)

    async def line(self, line_id: str) -> dict:
        payload = await self.management()
        line = next((item for item in payload["lines"] if item["line_id"] == line_id), None)
        if line is None:
            raise KeyError(line_id)
        return line

    async def history(self, line_id: str) -> dict:
        await self.line(line_id)
        async with self.detail_lock:
            cached = self.history_cache.get(line_id)
            if cached and time.monotonic() - cached[0] < 30:
                if cached[1] is None:
                    raise RuntimeError("HISTORY_UNAVAILABLE")
                return copy.deepcopy(cached[1])
            try:
                rows = await asyncio.wait_for(self.repository.history(line_id), 3)
            except Exception:
                self.history_cache[line_id] = (time.monotonic(), None)
                raise
            result = {"line_id": line_id, "checked_at": iso(), "events": [
                {"id": number(row.get("id")), "action": choice(row.get("action"), AUDIT_ACTIONS, "OTHER_REGISTRY_EVENT"),
                 "description": AUDIT_ACTIONS.get(row.get("action"), "Registry operation recorded"),
                 "operator_id": number(row.get("actor_staff_id")),
                 "at": iso(row["created_at"]) if isinstance(row.get("created_at"), datetime) else None}
                for row in rows[:30]]}
            self.history_cache[line_id] = (time.monotonic(), result)
            return copy.deepcopy(result)

    async def assessment(self, line_id: str) -> dict:
        line = await self.line(line_id)
        if line["lifecycle"] == "decommissioned":
            return {"line_id": line_id, "status": "BLOCKED", "recovery_allowed": False,
                    "checked_at": iso(), "blockers": ["IDENTITY_DECOMMISSIONED"],
                    "notice": "These credentials were permanently abandoned and cannot be reused."}
        async with self.detail_lock:
            cached = self.assessment_cache.get(line_id)
            if cached and time.monotonic() - cached[0] < 5:
                if cached[1] is None:
                    raise RuntimeError("ASSESSMENT_UNAVAILABLE")
                return copy.deepcopy(cached[1])
            try:
                result = {"line_id": line_id, "checked_at": iso(),
                          **sanitize_assessment(await asyncio.wait_for(self.client.get_auth_recovery(line_id), 3))}
            except Exception:
                self.assessment_cache[line_id] = (time.monotonic(), None)
                raise
            self.assessment_cache[line_id] = (time.monotonic(), result)
            return copy.deepcopy(result)


bridge_management_service = BridgeManagementService()
