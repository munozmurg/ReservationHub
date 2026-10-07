"""Publish reservations as an .ics calendar.

Each reservation's UID is its reservation_key, so re-importing or subscribing
updates events instead of duplicating them; cancellations are emitted with
STATUS:CANCELLED. Subscribe Google/Apple Calendar to this file's URL, or
import it. (For instant updates, swap this for the Google Calendar API.)
"""
import os
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TZ = os.environ.get("RESTAURANT_TZ", "America/New_York")
TURN_MINUTES = {2: 90, 4: 105}  # rough table-turn time by party size


def _esc(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def write_ics(con, path=None):
    path = path or os.path.join(ROOT, "exports", "reservations.ics")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = con.execute("""
        SELECT reservation_key, guest_name, party_size, starts_at, phone, email, booked_via,
               status, coalesce(guest_confirmation, 'unconfirmed'), notes, last_event_at
        FROM reservations WHERE starts_at >= current_date ORDER BY starts_at""").fetchall()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//reservation-hub//EN",
             "X-WR-CALNAME:Reservations (all sources)", f"X-WR-TIMEZONE:{TZ}"]
    for key, name, size, start, phone, email, via, status, conf, notes, changed in rows:
        minutes = TURN_MINUTES.get(size, 120) if size and size <= 4 else 120
        fmt = "%Y%m%dT%H%M%S"
        icon = {"yes": "✅", "no": "❌", "sent": "📨"}.get(conf, "•")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{key}@reservation-hub",
            f"DTSTAMP:{changed.strftime(fmt)}",
            f"DTSTART;TZID={TZ}:{start.strftime(fmt)}",
            f"DTEND;TZID={TZ}:{(start + timedelta(minutes=minutes)).strftime(fmt)}",
            f"SUMMARY:{_esc(f'{icon} {name} ({size}) · {via}')}",
            f"DESCRIPTION:{_esc(f'Phone: {phone}  Email: {email}  Confirmation: {conf}  Notes: {notes or chr(8212)}')}",
            f"STATUS:{'CANCELLED' if status == 'cancelled' else 'CONFIRMED'}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    with open(path, "w") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return path, len(rows)
