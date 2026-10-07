"""Parsers for bulk exports: Resy CSV, OpenTable CSV, website bookings JSON.

Exports are point-in-time snapshots, so every row becomes a `snapshot` event
stamped with the export time (taken from the filename, e.g.
resy_export_2026-10-07T1400.csv, falling back to the file's mtime).
"""
import csv
import json
import os
import re
from datetime import datetime

from hub.models import make_event, parse_starts_at, validate


def export_time(path):
    m = re.search(r"(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})", os.path.basename(path))
    if m:
        return f"{m.group(1)} {m.group(2)}:{m.group(3)}:00"
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")


def _status(raw):
    raw = (raw or "").strip().lower()
    return "cancelled" if raw.startswith("cancel") else "booked"


def parse_resy_csv(path):
    at = export_time(path)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            yield validate(make_event(
                source_platform="resy", source_channel="csv", event_type="snapshot",
                external_id=row["Confirmation"].strip(), event_at=at,
                guest_name=row["Guest Name"], phone=row["Phone"], email=row["Email"],
                party_size=row["Party Size"], starts_at=parse_starts_at(row["Date"], row["Time"]),
                table_id=row.get("Table") or None, status=_status(row["Status"]),
                notes=row.get("Notes") or None, source_file=os.path.basename(path),
            ))


def parse_opentable_csv(path):
    at = export_time(path)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            yield validate(make_event(
                source_platform="opentable", source_channel="csv", event_type="snapshot",
                external_id=row["Conf #"].strip(), event_at=at,
                guest_name=f"{row['First Name']} {row['Last Name']}".strip(),
                phone=row["Phone"], email=row["Email"], party_size=row["Covers"],
                starts_at=parse_starts_at(row["Visit Date"], row["Visit Time"]),
                status=_status(row["Status"]), notes=row.get("Guest Notes") or None,
                source_file=os.path.basename(path),
            ))


def parse_website_json(path):
    with open(path) as f:
        rows = json.load(f)
    for row in rows:
        yield validate(make_event(
            source_platform="website", source_channel="json", event_type="request",
            external_id=row["id"], event_at=row["submitted_at"].replace("T", " "),
            guest_name=row["name"], phone=row.get("phone"), email=row.get("email"),
            party_size=row["party_size"], starts_at=parse_starts_at(row["datetime"]),
            status="booked", notes=row.get("notes"), source_file=os.path.basename(path),
        ))


def detect_and_parse(path):
    """Route a landed file to its parser based on name/extension."""
    name = os.path.basename(path).lower()
    if name.endswith(".csv") and "resy" in name:
        return list(parse_resy_csv(path))
    if name.endswith(".csv") and "opentable" in name:
        return list(parse_opentable_csv(path))
    if name.endswith(".json") and "website" in name:
        return list(parse_website_json(path))
    if name.endswith(".eml"):
        from hub.sources.email_inbox import parse_eml
        return parse_eml(path)
    raise ValueError(f"No parser for {name}")
