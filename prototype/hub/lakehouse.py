"""Local lakehouse: open file formats (Parquet) + a DuckDB catalog.

Medallion layout under lakehouse/:

  bronze/raw/<platform>/<ingest_date>/<hash>_<file>   immutable copy of every source file
  silver/reservation_events/ingest_date=<d>/*.parquet  append-only normalized events
  silver/reservations.parquet                          current state, rebuilt from events
  gold/*.parquet                                       run sheet, review queue, duplicates, covers
  catalog.duckdb                                       ingest manifest, sync runs, views

Swap the storage root for S3/GCS and DuckDB for Databricks/Snowflake/Iceberg
later; the layer contract stays the same.
"""
import hashlib
import os
import shutil
import uuid
from datetime import date

import duckdb

from hub.models import EVENT_FIELDS, now_iso

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lakehouse")
EVENTS_GLOB = os.path.join(ROOT, "silver", "reservation_events", "*", "*.parquet")

COLUMN_TYPES = {
    "party_size": "INTEGER",
    "needs_review": "BOOLEAN",
    "event_at": "TIMESTAMP",
    "starts_at": "TIMESTAMP",
    "ingested_at": "TIMESTAMP",
}


def connect():
    os.makedirs(ROOT, exist_ok=True)
    con = duckdb.connect(os.path.join(ROOT, "catalog.duckdb"))
    con.execute("""
        CREATE TABLE IF NOT EXISTS ingest_manifest (
            file_hash TEXT PRIMARY KEY, file_name TEXT, platform TEXT,
            bronze_path TEXT, events INTEGER, ingested_at TIMESTAMP)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS sync_runs (
            run_id TEXT PRIMARY KEY, started_at TIMESTAMP, finished_at TIMESTAMP,
            status TEXT, files_ingested INTEGER, events_written INTEGER, error TEXT)""")
    return con


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def already_ingested(con, digest):
    return con.execute("SELECT 1 FROM ingest_manifest WHERE file_hash = ?", [digest]).fetchone() is not None


def land_bronze(path, platform, digest):
    """Copy the untouched source file into bronze so it can always be replayed."""
    d = os.path.join(ROOT, "bronze", "raw", platform, date.today().isoformat())
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, f"{digest[:12]}_{os.path.basename(path)}")
    shutil.copy2(path, dest)
    return dest


def write_events(con, events):
    """Append a batch of events to silver as one Parquet file."""
    if not events:
        return None
    cols = ", ".join(f"{c} {COLUMN_TYPES.get(c, 'TEXT')}" for c in EVENT_FIELDS)
    con.execute(f"CREATE OR REPLACE TEMP TABLE _batch ({cols})")
    con.executemany(
        f"INSERT INTO _batch VALUES ({', '.join('?' for _ in EVENT_FIELDS)})",
        [[e[c] for c in EVENT_FIELDS] for e in events],
    )
    d = os.path.join(ROOT, "silver", "reservation_events", f"ingest_date={date.today().isoformat()}")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, f"batch_{uuid.uuid4().hex[:12]}.parquet")
    con.execute(f"COPY _batch TO '{out}' (FORMAT PARQUET)")
    return out


def record_manifest(con, digest, path, platform, bronze_path, n):
    con.execute("INSERT INTO ingest_manifest VALUES (?, ?, ?, ?, ?, ?)",
                [digest, os.path.basename(path), platform, bronze_path, n, now_iso()])


def rebuild_silver_and_gold(con):
    """Derive current state from the full event log, then publish gold tables.

    Rules:
      * Events for one reservation_key are ordered by event_at (when it happened
        at the source), ties broken by source trust: sms > host > email/json > csv.
      * Each field takes its latest non-null value, so a cancel email that only
        carries the confirmation number doesn't wipe out the guest's phone.
    """
    if not os.path.exists(os.path.join(ROOT, "silver", "reservation_events")):
        return
    silver = os.path.join(ROOT, "silver", "reservations.parquet")
    gold = os.path.join(ROOT, "gold")
    os.makedirs(gold, exist_ok=True)

    con.execute(f"""
        CREATE OR REPLACE VIEW reservation_events AS
        SELECT * FROM read_parquet('{EVENTS_GLOB}', hive_partitioning = true, union_by_name = true)""")

    def latest(col):
        return f"arg_max({col}, rn) FILTER (WHERE {col} IS NOT NULL) AS {col}"

    con.execute(f"""
        COPY (
          WITH ev AS (
            SELECT *, row_number() OVER (
              PARTITION BY reservation_key
              ORDER BY event_at,
                       CASE source_channel WHEN 'csv' THEN 0 WHEN 'json' THEN 1 WHEN 'web' THEN 1 WHEN 'email' THEN 1
                                           WHEN 'manual' THEN 3 WHEN 'sms' THEN 4 ELSE 0 END,
                       ingested_at) AS rn
            FROM reservation_events)
          SELECT reservation_key,
                 arg_min(source_platform, rn) AS booked_via,
                 arg_min(external_id, rn) FILTER (WHERE external_id IS NOT NULL) AS external_id,
                 {latest('guest_name')}, {latest('phone')}, {latest('email')},
                 {latest('party_size')}, {latest('starts_at')}, {latest('table_id')},
                 {latest('notes')}, {latest('status')}, {latest('guest_confirmation')},
                 arg_max(event_type, rn) AS last_event_type,
                 max(event_at) AS last_event_at,
                 string_agg(DISTINCT source_channel, ',' ORDER BY source_channel) AS seen_in,
                 bool_or(needs_review) AS needs_review,
                 string_agg(DISTINCT review_reason, '; ') AS review_reason,
                 count(*) AS event_count
          FROM ev GROUP BY reservation_key
        ) TO '{silver}' (FORMAT PARQUET)""")
    con.execute(f"CREATE OR REPLACE VIEW reservations AS SELECT * FROM read_parquet('{silver}')")

    con.execute(f"""
        COPY (
          SELECT starts_at, guest_name, party_size, phone, email, booked_via, external_id,
                 table_id, coalesce(guest_confirmation, 'unconfirmed') AS guest_confirmation,
                 notes, needs_review, reservation_key
          FROM reservations
          WHERE status = 'booked' AND starts_at >= current_date
          ORDER BY starts_at, guest_name
        ) TO '{gold}/upcoming_reservations.parquet' (FORMAT PARQUET)""")

    con.execute(f"""
        COPY (
          SELECT reservation_key, booked_via, guest_name, starts_at, review_reason, last_event_at
          FROM reservations WHERE needs_review ORDER BY last_event_at
        ) TO '{gold}/review_queue.parquet' (FORMAT PARQUET)""")

    # Same guest, same night, within 90 minutes, different booking keys:
    # classic double-booking across platforms (e.g. Resy AND OpenTable).
    con.execute(f"""
        COPY (
          SELECT a.reservation_key AS key_a, b.reservation_key AS key_b,
                 a.guest_name, a.starts_at AS starts_a, b.starts_at AS starts_b,
                 a.booked_via AS via_a, b.booked_via AS via_b
          FROM reservations a JOIN reservations b
            ON a.reservation_key < b.reservation_key
           AND a.status = 'booked' AND b.status = 'booked'
           AND CAST(a.starts_at AS DATE) = CAST(b.starts_at AS DATE)
           AND abs(epoch(a.starts_at) - epoch(b.starts_at)) <= 90 * 60
           AND (a.phone = b.phone OR a.email = b.email)
        ) TO '{gold}/possible_duplicates.parquet' (FORMAT PARQUET)""")

    con.execute(f"""
        COPY (
          SELECT CAST(starts_at AS DATE) AS service_date, booked_via,
                 count(*) AS reservations, CAST(sum(party_size) AS INTEGER) AS covers
          FROM reservations WHERE status = 'booked'
          GROUP BY ALL ORDER BY ALL
        ) TO '{gold}/covers_by_day_and_source.parquet' (FORMAT PARQUET)""")

    for name in ["upcoming_reservations", "review_queue", "possible_duplicates", "covers_by_day_and_source"]:
        con.execute(f"CREATE OR REPLACE VIEW gold_{name} AS SELECT * FROM read_parquet('{gold}/{name}.parquet')")
