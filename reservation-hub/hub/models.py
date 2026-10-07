"""Canonical reservation event model shared by every source.

Every source (Resy, OpenTable, website, email, SMS replies, host edits) is
normalized into the same flat event record. The lakehouse never stores
"current state" from a source directly; it stores events and derives state.
"""
import hashlib
import re
from datetime import datetime, timezone

EVENT_FIELDS = [
    "reservation_key",   # stable id across all events for one booking
    "source_platform",   # resy | opentable | website | email | sms | host
    "source_channel",    # csv | json | email | sms | manual
    "external_id",       # platform confirmation number, if any
    "event_type",        # snapshot | created | modified | cancelled | request | confirmation_sent | confirmed | declined
    "event_at",          # when the change happened at the source (ISO)
    "guest_name",
    "phone",             # E.164
    "email",
    "party_size",
    "starts_at",         # restaurant-local ISO datetime "YYYY-MM-DD HH:MM:SS"
    "table_id",
    "status",            # booked | cancelled | None (event doesn't change status)
    "guest_confirmation",  # sent | yes | no | None
    "notes",
    "needs_review",
    "review_reason",
    "source_file",
    "ingested_at",
]

# Tie-break when two events share the same event_at: higher wins.
SOURCE_PRIORITY = {"snapshot": 0, "email": 1, "json": 1, "csv": 0, "manual": 3, "sms": 4}


def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")


def normalize_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else None


def normalize_email(raw):
    raw = (raw or "").strip().lower()
    return raw or None


_TIME_FORMATS = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"]
_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%a %b %d %Y", "%B %d %Y", "%b %d %Y"]


def parse_starts_at(date_str, time_str=None):
    """Combine date + time strings in the many formats sources use."""
    if time_str is None:
        combined = date_str.strip()
        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]:
            try:
                return datetime.strptime(combined, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        raise ValueError(f"Unrecognized datetime: {date_str!r}")
    d = None
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(date_str.strip().replace(",", ""), fmt)
            break
        except ValueError:
            pass
    t = None
    for fmt in _TIME_FORMATS:
        try:
            t = datetime.strptime(time_str.strip().upper(), fmt)
            break
        except ValueError:
            pass
    if d is None or t is None:
        raise ValueError(f"Unrecognized date/time: {date_str!r} {time_str!r}")
    return d.replace(hour=t.hour, minute=t.minute).strftime("%Y-%m-%d %H:%M:%S")


def fallback_key(platform, phone, email, starts_at):
    """Key for bookings that arrive without a platform confirmation number."""
    basis = f"{phone or ''}|{email or ''}|{starts_at or ''}"
    return f"{platform}:h{hashlib.sha1(basis.encode()).hexdigest()[:10]}"


def make_event(**kwargs):
    ev = {f: None for f in EVENT_FIELDS}
    ev.update(kwargs)
    ev["phone"] = normalize_phone(ev["phone"])
    ev["email"] = normalize_email(ev["email"])
    if ev["party_size"] not in (None, ""):
        ev["party_size"] = int(ev["party_size"])
    else:
        ev["party_size"] = None
    if not ev["reservation_key"]:
        if ev["external_id"]:
            ev["reservation_key"] = f"{ev['source_platform']}:{ev['external_id']}"
        else:
            ev["reservation_key"] = fallback_key(ev["source_platform"], ev["phone"], ev["email"], ev["starts_at"])
    ev["needs_review"] = bool(ev["needs_review"])
    ev["ingested_at"] = ev["ingested_at"] or now_iso()
    return ev


def validate(ev):
    """Flag events staff should eyeball instead of silently trusting them."""
    reasons = []
    if ev["event_type"] in ("snapshot", "created", "request"):
        if not ev["starts_at"]:
            reasons.append("missing date/time")
        if not ev["phone"] and not ev["email"]:
            reasons.append("no way to contact guest")
        if ev["party_size"] is not None and not (1 <= ev["party_size"] <= 20):
            reasons.append(f"unusual party size {ev['party_size']}")
    if reasons:
        ev["needs_review"] = True
        ev["review_reason"] = "; ".join(filter(None, [ev.get("review_reason"), *reasons]))
    return ev
