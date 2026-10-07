"""Guest confirmation texts via Twilio, plus reply handling.

DRY RUN BY DEFAULT: messages are written to outbox/sms_outbox.jsonl and not
sent. Real sending needs --send AND these environment variables:
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.

Every send and every reply is written back to the lakehouse as an event, so
"who confirmed" is part of the same history as the booking itself.
"""
import base64
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

from hub.models import make_event, normalize_phone, now_iso

OUTBOX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outbox")
RESTAURANT = os.environ.get("RESTAURANT_NAME", "Casa Demo")


def message_for(row):
    when = row["starts_at"].strftime("%-I:%M%p").lower()
    first = (row["guest_name"] or "there").split()[0]
    return (f"Hi {first}, this is {RESTAURANT}. Confirming your table for {row['party_size']} "
            f"tonight at {when}. Reply YES to confirm or NO to cancel.")


def _twilio_send(to, body):
    sid, token, sender = (os.environ.get(k) for k in
                          ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))
    if not all([sid, token, sender]):
        raise RuntimeError("Twilio env vars missing; run without --send for a dry run")
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=urllib.parse.urlencode({"To": to, "From": sender, "Body": body}).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["sid"]


def send_confirmations(con, service_date, send=False):
    """Text every booked, not-yet-contacted guest for the given night."""
    rows = con.execute("""
        SELECT reservation_key, guest_name, phone, party_size, starts_at
        FROM reservations
        WHERE status = 'booked' AND CAST(starts_at AS DATE) = ?
          AND coalesce(guest_confirmation, 'unconfirmed') = 'unconfirmed'
        ORDER BY starts_at""", [service_date]).fetchall()
    cols = ["reservation_key", "guest_name", "phone", "party_size", "starts_at"]
    os.makedirs(OUTBOX, exist_ok=True)
    events, skipped = [], []
    for r in (dict(zip(cols, r)) for r in rows):
        if not r["phone"]:
            skipped.append(r["guest_name"])
            continue
        body = message_for(r)
        sid = _twilio_send(r["phone"], body) if send else "dry-run"
        with open(os.path.join(OUTBOX, "sms_outbox.jsonl"), "a") as f:
            f.write(json.dumps({"at": now_iso(), "to": r["phone"], "body": body, "sid": sid}) + "\n")
        events.append(make_event(
            reservation_key=r["reservation_key"], source_platform="sms", source_channel="sms",
            event_type="confirmation_sent", event_at=now_iso(), guest_confirmation="sent",
            phone=r["phone"]))
    return events, skipped


def reply_event(con, from_phone, body):
    """Turn an inbound SMS into a confirmed/declined event for that guest's next booking."""
    phone = normalize_phone(from_phone)
    row = con.execute("""
        SELECT reservation_key FROM reservations
        WHERE phone = ? AND status = 'booked' AND starts_at >= current_date
        ORDER BY starts_at LIMIT 1""", [phone]).fetchone()
    if not row:
        return None
    words = re.findall(r"[a-z]+", body.lower())
    word = words[0] if words else ""
    if word in ("yes", "y", "confirm", "confirmed"):
        kind, conf, status = "confirmed", "yes", None
    elif word in ("no", "n", "cancel"):
        kind, conf, status = "declined", "no", "cancelled"
    else:
        return make_event(reservation_key=row[0], source_platform="sms", source_channel="sms",
                          event_type="reply_unclear", event_at=now_iso(), phone=phone,
                          needs_review=True, review_reason=f"unclear SMS reply: {body!r}")
    return make_event(reservation_key=row[0], source_platform="sms", source_channel="sms",
                      event_type=kind, event_at=now_iso(), guest_confirmation=conf,
                      status=status, phone=phone)
