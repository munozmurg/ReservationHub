"""Outage mode, pre-shift snapshot check, website bookings and guest outreach.

The idea: Resy can go down at any time, so before service we make sure the
hub holds a fresh copy of the book, and during an outage the restaurant's own
website keeps taking bookings against the hub's availability grid.
"""
import os
import uuid
from datetime import datetime

from hub.availability import alternatives, is_available, load_config
from hub.models import make_event, now_iso, validate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings(con):
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)")


def set_outage(con, on):
    _settings(con)
    con.execute("INSERT OR REPLACE INTO settings VALUES ('outage', ?, ?)", ["on" if on else "off", now_iso()])


def outage_on(con):
    _settings(con)
    row = con.execute("SELECT value FROM settings WHERE key = 'outage'").fetchone()
    return bool(row and row[0] == "on")


def preshift_check(con, as_of=None):
    """Is our latest copy of the Resy book fresh enough to run service from?"""
    cfg = load_config()
    as_of = as_of or datetime.now()
    row = con.execute("""
        SELECT max(event_at) FROM reservation_events
        WHERE source_platform = 'resy' AND event_type = 'snapshot'""").fetchone()
    last = row[0] if row else None
    if last is None:
        return False, "⚠ NO RESY SNAPSHOT. Export the book from Resy OS now, before service."
    age = (as_of - last).total_seconds() / 60
    if age > cfg["snapshot_max_age_minutes"]:
        return False, (f"⚠ STALE: last Resy snapshot is {age:.0f} min old ({last:%H:%M}). "
                       f"Export a fresh one before service.")
    return True, f"✓ Resy snapshot is {age:.0f} min old ({last:%H:%M}). Safe to run service from the hub."


def website_booking(con, name, phone, email, party, starts_at, notes=None):
    """A booking from the restaurant's own website, checked against our grid, not Resy's."""
    outage = outage_on(con)
    ok, tables, reason = is_available(con, party, starts_at, outage=outage)
    if not ok:
        alts = alternatives(con, party, starts_at, outage=outage)
        return None, reason, alts
    ev = validate(make_event(
        source_platform="website", source_channel="web", event_type="created",
        external_id=f"W-{uuid.uuid4().hex[:6].upper()}", event_at=now_iso(),
        guest_name=name, phone=phone, email=email, party_size=party,
        starts_at=starts_at.strftime("%Y-%m-%d %H:%M:%S"), table_id=tables[0],
        status="booked", notes=notes,
        review_reason="booked during Resy outage, re-enter in Resy when it's back" if outage else None,
        needs_review=outage,
    ))
    return ev, "ok", []


def outreach_plan(con, service_date):
    """Who we can text, who we can only email, and who we can't reach at all."""
    rows = con.execute("""
        SELECT guest_name, party_size, starts_at, phone, email, booked_via
        FROM reservations
        WHERE status = 'booked' AND CAST(starts_at AS DATE) = ?
        ORDER BY starts_at""", [service_date]).fetchall()
    plan = {"text": [], "email_only": [], "unreachable": []}
    for name, party, start, phone, email, via in rows:
        bucket = "text" if phone else "email_only" if email else "unreachable"
        plan[bucket].append(f"{start:%H:%M} {name} ({party}) via {via}")
    return plan


def write_public_notices(service_date):
    """Copy for channels that reach guests we have no contact for."""
    cfg = load_config()
    name, phone = cfg["restaurant_name"], cfg["public_phone"]
    text = f"""PUBLIC NOTICES for {service_date}

WEBSITE BANNER
Our reservation system is having issues tonight. Your booking is still valid.
Text or call {phone} with your name and time to confirm, or just come in, we'll have your table.

GOOGLE BUSINESS PROFILE POST / INSTAGRAM STORY
Heads up: our booking platform is down tonight. All reservations are honored.
Questions? Text {phone}. See you soon! — {name}

PHONE GREETING / VOICEMAIL
Thanks for calling {name}. Our online booking system is temporarily down, but
all reservations for tonight are still valid. To confirm, text your name and
reservation time to this number, or stay on the line.

HOST STAND
If a guest isn't on the run sheet but shows a Resy confirmation: honor it, seat
them from the buffer tables, and add them in the hub as a host booking.
"""
    os.makedirs(os.path.join(ROOT, "exports"), exist_ok=True)
    path = os.path.join(ROOT, "exports", f"outage_notices_{service_date}.txt")
    with open(path, "w") as f:
        f.write(text)
    return path
