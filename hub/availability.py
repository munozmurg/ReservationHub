"""The restaurant's own availability grid, independent of Resy.

Tables come from config/tables.csv, bookings from the hub's current state.
Bookings that already have a table keep it; the rest are placed greedily on
the smallest free table that fits. Anything that can't be placed is reported
as an overbooking.

In outage mode a share of tables (outage_buffer_pct) is held back for bookings
we may not know about yet, so the website can't sell the last seats.
"""
import csv
import json
import re
import math
import os
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    with open(os.path.join(ROOT, "config", "service.json")) as f:
        cfg = json.load(f)
    with open(os.path.join(ROOT, "config", "tables.csv"), newline="") as f:
        cfg["tables"] = sorted(
            ({"table_id": r["table_id"], "seats": int(r["seats"])} for r in csv.DictReader(f)),
            key=lambda t: (t["seats"], [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", t["table_id"])]))
    return cfg


def turn_minutes(cfg, party):
    sizes = sorted(int(k) for k in cfg["turn_minutes"])
    fit = next((s for s in sizes if party <= s), sizes[-1])
    return cfg["turn_minutes"][str(fit)]


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def build_grid(con, service_date, cfg=None):
    """Return ({table_id: [(start, end, guest)]}, [unplaced bookings])."""
    cfg = cfg or load_config()
    rows = con.execute("""
        SELECT guest_name, party_size, starts_at, table_id FROM reservations
        WHERE status = 'booked' AND CAST(starts_at AS DATE) = ?
        ORDER BY table_id IS NULL, starts_at""", [service_date]).fetchall()
    grid = {t["table_id"]: [] for t in cfg["tables"]}
    unplaced = []
    for guest, party, start, table in rows:
        party = party or 2
        end = start + timedelta(minutes=turn_minutes(cfg, party))
        if table in grid:
            grid[table].append((start, end, guest))
            continue
        for t in cfg["tables"]:
            if t["seats"] >= party and not any(_overlaps(start, end, s, e) for s, e, _ in grid[t["table_id"]]):
                grid[t["table_id"]].append((start, end, guest))
                break
        else:
            unplaced.append((guest, party, start))
    return grid, unplaced


def free_tables(grid, cfg, party, start):
    end = start + timedelta(minutes=turn_minutes(cfg, party))
    return [t["table_id"] for t in cfg["tables"]
            if t["seats"] >= party and not any(_overlaps(start, end, s, e) for s, e, _ in grid[t["table_id"]])]


def is_available(con, party, start, outage=False, cfg=None):
    """Can we seat `party` at `start`? Returns (ok, table_ids, reason)."""
    cfg = cfg or load_config()
    grid, _ = build_grid(con, start.date().isoformat(), cfg)
    fits = free_tables(grid, cfg, party, start)
    if not fits:
        return False, [], "no table that size is free"
    if outage:
        # Hold back a buffer: the last N free tables are not sellable while Resy is down.
        total_free = sum(1 for t in cfg["tables"] if not any(
            _overlaps(start, start + timedelta(minutes=turn_minutes(cfg, party)), s, e)
            for s, e, _ in grid[t["table_id"]]))
        held = math.ceil(len(cfg["tables"]) * cfg["outage_buffer_pct"])
        if total_free <= held:
            return False, fits, f"outage buffer: {total_free} free table(s), {held} held back"
    return True, fits, "ok"


def slots(cfg, service_date):
    day = datetime.strptime(service_date, "%Y-%m-%d")
    t = datetime.combine(day, datetime.strptime(cfg["service_start"], "%H:%M").time())
    last = datetime.combine(day, datetime.strptime(cfg["last_seating"], "%H:%M").time())
    while t <= last:
        yield t
        t += timedelta(minutes=cfg["slot_minutes"])


def alternatives(con, party, wanted, outage=False, n=3):
    cfg = load_config()
    options = [s for s in slots(cfg, wanted.date().isoformat()) if is_available(con, party, s, outage, cfg)[0]]
    return sorted(options, key=lambda s: abs((s - wanted).total_seconds()))[:n]


def print_grid(con, service_date, outage=False):
    cfg = load_config()
    grid, unplaced = build_grid(con, service_date, cfg)
    times = list(slots(cfg, service_date))[::2]  # every 30 min keeps it readable
    print("table  " + " ".join(t.strftime("%H:%M") for t in times))
    for t in cfg["tables"]:
        cells = []
        for s in times:
            busy = any(st <= s < en for st, en, _ in grid[t["table_id"]])
            cells.append("  ██ " if busy else "  ·  ")
        print(f"{t['table_id']:<4}{t['seats']:>2} " + " ".join(cells))
    held = math.ceil(len(cfg["tables"]) * cfg["outage_buffer_pct"])
    if outage:
        print(f"OUTAGE MODE: {held} table(s) held back as buffer at every time slot")
    for guest, party, start in unplaced:
        print(f"⚠ OVERBOOKED: {guest} ({party}) at {start:%H:%M} doesn't fit any free table")
