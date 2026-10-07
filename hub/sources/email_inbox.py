"""Parse reservation emails (.eml) from Resy, OpenTable, the website form,
and free-text guest emails.

Platform notification emails follow fixed templates, so a "Key: value"
extractor plus per-platform field aliases covers them. Anything we can't
parse confidently becomes a `request` event flagged for human review rather
than being dropped.
"""
import email
import os
import re
from email import policy
from email.utils import parsedate_to_datetime

from hub.models import make_event, parse_starts_at, validate

ALIASES = {
    "external_id": ["confirmation", "confirmation #", "conf #", "reservation id", "request id"],
    "guest_name": ["guest", "guest name", "name", "diner"],
    "phone": ["phone", "phone number", "mobile"],
    "email": ["email", "e-mail"],
    "party_size": ["party size", "covers", "guests", "party"],
    "date": ["date", "visit date"],
    "time": ["time", "visit time"],
    "notes": ["notes", "special requests", "guest notes", "message"],
}


def _kv(body):
    out = {}
    for line in body.splitlines():
        m = re.match(r"\s*([A-Za-z #\-]+?)\s*:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1).strip().lower()] = m.group(2)
    fields = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in out:
                fields[field] = out[n]
                break
    return fields


def _platform(sender, subject):
    s = f"{sender} {subject}".lower()
    if "resy" in s:
        return "resy"
    if "opentable" in s:
        return "opentable"
    if "website" in s or "forms@" in s:
        return "website"
    return "email"


def _event_type(subject):
    s = subject.lower()
    if "cancel" in s:
        return "cancelled"
    if "modif" in s or "updated" in s or "changed" in s:
        return "modified"
    if "new reservation" in s or "booked" in s:
        return "created"
    return "request"


def parse_eml(path):
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    sender, subject = str(msg["From"] or ""), str(msg["Subject"] or "")
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    sent_at = parsedate_to_datetime(msg["Date"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    platform = _platform(sender, subject)
    etype = _event_type(subject)
    f = _kv(body)

    starts_at, review = None, None
    if f.get("date") and f.get("time"):
        try:
            starts_at = parse_starts_at(f["date"], f["time"])
        except ValueError as e:
            review = str(e)

    if platform == "email":
        # Free-text guest email ("can I get a table for 3 at 7?"). Don't guess:
        # keep the raw text and send it to the review queue. Production would add
        # an LLM extraction step here with a confidence threshold.
        sender_addr = email.utils.parseaddr(sender)
        return [make_event(
            source_platform="email", source_channel="email", event_type="request",
            event_at=sent_at, guest_name=sender_addr[0] or None, email=sender_addr[1],
            notes=f"{subject}: {body.strip()[:500]}", needs_review=True,
            review_reason="free-text email, needs a human to confirm details",
            source_file=os.path.basename(path),
        )]

    status = {"cancelled": "cancelled", "created": "booked", "modified": "booked", "request": "booked"}[etype]
    ev = make_event(
        source_platform=platform, source_channel="email", event_type=etype,
        external_id=(f.get("external_id") or "").strip() or None, event_at=sent_at,
        guest_name=f.get("guest_name"), phone=f.get("phone"), email=f.get("email"),
        party_size=re.sub(r"\D", "", f.get("party_size", "")) or None,
        starts_at=starts_at, status=status, notes=f.get("notes"),
        needs_review=bool(review), review_reason=review, source_file=os.path.basename(path),
    )
    if not ev["external_id"] and etype in ("cancelled", "modified"):
        ev["needs_review"] = True
        ev["review_reason"] = "change email without confirmation number"
    return [validate(ev)]
