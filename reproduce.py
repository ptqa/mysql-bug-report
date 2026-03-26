#!/usr/bin/env python3
"""
Reproduction script for MySQL crash in json_binary::parse_binary
when using multi-value JSON indexes with index merge.

Crashes MySQL 8.0.42 (SIGSEGV). Returns error on 8.0.45 (same root cause).

The crash is non-deterministic (~20% per attempt) because it depends on
InnoDB cursor restore behavior after mtr restart. The script loops up to
MAX_ATTEMPTS times to trigger it reliably.

Prerequisites: pip install mysql-connector-python
Usage: docker compose up -d && sleep 60 && python3 reproduce.py
"""

import mysql.connector
import sys
import time

HOST = "127.0.0.1"
PORT = 3307
USER = "root"
PASSWD = "root"
DB = "testdb"

MV_VALUE = 99999
NUM_INVISIBLE_ROWS = 5000
MAX_ATTEMPTS = 20


def get_conn(**kwargs):
    return mysql.connector.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWD,
        database=DB,
        autocommit=False,
        **kwargs,
    )


def wait_for_mysql(timeout=120):
    """Wait until MySQL is ready to accept connections."""
    print("Waiting for MySQL to be ready...", end="", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        try:
            c = get_conn(connection_timeout=5)
            c.close()
            print(" ready.")
            return True
        except Exception:
            print(".", end="", flush=True)
            time.sleep(2)
    print(" timeout!")
    return False


def cleanup():
    """Remove rows from previous runs."""
    c = get_conn()
    c.autocommit = True
    cur = c.cursor()
    cur.execute(f"DELETE FROM calendar_events WHERE {MV_VALUE} MEMBER OF(groupclients)")
    cur.execute(f"DELETE FROM calendar_events WHERE inquiry_id = {MV_VALUE}")
    c.close()


def single_attempt():
    """
    Run one crash attempt. Returns:
      'crash'   - server died (SIGSEGV)
      'error'   - query returned ER_INVALID_JSON_BINARY_DATA (8.0.45+)
      'no_bug'  - query succeeded without error
    """
    cleanup()

    # Step 1: snapshot before inserts
    conn_a = get_conn()
    cur_a = conn_a.cursor()
    cur_a.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

    # Step 2: insert MVCC-invisible rows
    conn_b = get_conn()
    conn_b.autocommit = True
    cur_b = conn_b.cursor()
    batch = 500
    for i in range(0, NUM_INVISIBLE_ROWS, batch):
        vals = []
        for j in range(i, min(i + batch, NUM_INVISIBLE_ROWS)):
            vals.append(
                f"(0,'active',"
                f"DATE_ADD('2026-02-01',INTERVAL {j % 365} DAY),'09:00:00',"
                f"DATE_ADD('2026-02-01',INTERVAL {j % 365} DAY),'10:00:00',"
                f"'UTC','T{j}','meeting',1,'A','B',"
                f"{MV_VALUE},JSON_ARRAY({MV_VALUE}))"
            )
        cur_b.execute(
            "INSERT INTO calendar_events "
            "(eie,status,start_date,start_time,end_date,end_time,"
            "timezone,title,event_type,staff_id,sfname,slname,"
            "inquiry_id,groupclients) VALUES " + ",".join(vals)
        )
    conn_b.close()

    # Step 3: run the crashing query
    query = f"""
    SELECT /*+ INDEX_MERGE(calendar_events
               calendar_events_inquiry_id_index,
               calendar_events_groupclients_index) */
           start_date, start_time, end_date, end_time, timezone,
           staff_id, sfname, slname, status, title, event_type,
           start_date as datedoc
    FROM calendar_events
    WHERE (inquiry_id = {MV_VALUE}
           OR json_contains(groupclients, '{MV_VALUE}'))
      AND eie = 0
      AND status <> 'deleted'
      AND date(calendar_events.start_date) >= '2026-01-31'
    ORDER BY start_date DESC, start_time DESC
    LIMIT 10
    """

    try:
        cur_a.execute(query)
        cur_a.fetchall()
        conn_a.rollback()
        conn_a.close()
        return "no_bug"
    except mysql.connector.errors.DatabaseError as e:
        err = str(e)
        if "Lost connection" in err or "gone away" in err:
            return "crash"
        elif "invalid" in err.lower() and "json" in err.lower():
            try:
                conn_a.rollback()
                conn_a.close()
            except Exception:
                pass
            return "error"
        else:
            try:
                conn_a.rollback()
                conn_a.close()
            except Exception:
                pass
            return f"unexpected: {e}"
    except Exception as e:
        try:
            test = get_conn(connection_timeout=3)
            test.close()
            return f"unexpected: {e}"
        except Exception:
            return "crash"


def main():
    print("=" * 70)
    print("BUG: SIGSEGV in json_binary::parse_binary during IndexMerge")
    print("     on multi-value JSON index with MVCC-invisible rows")
    print("=" * 70)
    print()

    if not wait_for_mysql():
        print("ERROR: MySQL is not available on port 3307.")
        print("Run: docker compose up -d && sleep 60")
        return 1

    c = get_conn()
    c.autocommit = True
    cur = c.cursor()
    cur.execute("SELECT VERSION()")
    version = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM calendar_events")
    total = cur.fetchone()[0]
    c.close()
    print(f"MySQL version: {version}")
    print(f"Baseline rows: {total}")
    print()

    # The crash is non-deterministic (~20% per attempt) because it depends
    # on whether InnoDB's cursor restore keeps prev_rec valid after the
    # mini-transaction restart following a clustered index MVCC lookup.
    # We loop up to MAX_ATTEMPTS times to trigger it reliably.

    print(
        f"Running up to {MAX_ATTEMPTS} attempts "
        f"(inserting {NUM_INVISIBLE_ROWS} MVCC-invisible rows each time)..."
    )
    print()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        sys.stdout.write(f"  Attempt {attempt}/{MAX_ATTEMPTS}... ")
        sys.stdout.flush()

        result = single_attempt()

        if result == "crash":
            print("CRASHED!")
            print()
            print("=" * 70)
            print(f"SERVER CRASHED (SIGSEGV) on attempt {attempt}")
            print("=" * 70)
            print()
            print("The crash occurs in this call chain:")
            print("  IndexMergeIterator::Init")
            print("    -> IndexRangeScanIterator::Read")
            print("      -> handler::multi_range_read_next")
            print("        -> handler::read_range_first")
            print("          -> row_search_mvcc")
            print("            -> row_search_end_range_check")
            print("              -> handler::compare_key_in_buffer  <-- missing guard")
            print("                -> key_cmp")
            print("                  -> Field_typed_array::key_cmp")
            print("                    -> Field_json::val_json")
            print("                      -> json_binary::parse_binary  <-- SIGSEGV")
            print()
            print("Check crash log:")
            print("  docker logs mysql_mv_bug 2>&1 | grep -A 40 'mysqld got signal'")
            print()
            print("Waiting for MySQL to restart...", end="", flush=True)
            time.sleep(5)
            wait_for_mysql(timeout=60)
            return 1

        elif result == "error":
            print("ERROR (ER_INVALID_JSON_BINARY_DATA)")
            print()
            print("=" * 70)
            print(f"QUERY RETURNED ERROR on attempt {attempt}")
            print("=" * 70)
            print()
            print("Same root cause as the SIGSEGV, but json_binary::parse_binary")
            print("is hardened in this version and returns an error instead of")
            print("crashing. The query should return 0 rows, not an error.")
            return 1

        elif result == "no_bug":
            print("no crash (will retry)")
        else:
            print(f"unexpected: {result}")

    print()
    print(f"Bug did not trigger in {MAX_ATTEMPTS} attempts.")
    print("Try increasing NUM_INVISIBLE_ROWS or running again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
