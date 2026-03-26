# SIGSEGV in json_binary::parse_binary with multi-value index + IndexMerge + MVCC

## Summary

`mysqld` crashes with SIGSEGV when all three conditions are met:

1. A table has a **multi-value JSON index** (`CAST(col AS UNSIGNED ARRAY)`)
2. The optimizer chooses an **IndexMerge** (sort_union) plan that includes that index
3. The multi-value index range contains enough **MVCC-invisible rows** (~100+)

**Affected versions:**
- 8.0.42: SIGSEGV (server crash)
- 8.0.44: SIGSEGV (server crash)
- 8.0.45: SIGSEGV (server crash, latest release as of March 2026)
- 8.4 / 9.x: not tested, likely affected (the buggy code is unchanged)

## Reproduction

```bash
# Prerequisites: Docker, Python 3, mysql-connector-python
pip install mysql-connector-python

# Start MySQL 8.0.45 (seeding 200K rows takes ~30 seconds)
docker compose up -d
sleep 60

# Reproduce the crash
python3 reproduce.py
```

Expected output:
```
Running up to 20 attempts (inserting 5000 MVCC-invisible rows each time)...

  Attempt 1/20... no crash (will retry)
  ...
  Attempt N/20... CRASHED!

======================================================================
SERVER CRASHED (SIGSEGV) on attempt N
======================================================================
```

The crash is **non-deterministic** (~20% per attempt) because it depends on
whether InnoDB's cursor restore keeps `prev_rec` valid after a mini-transaction
restart. The script loops up to 20 times and typically crashes within 3-10 attempts.

Tested and confirmed on 8.0.42, 8.0.44, and 8.0.45.

## Crash stack trace

```
Field_json::val_json(Json_wrapper*) const
Field_typed_array::key_cmp(unsigned char const*, unsigned int) const
key_cmp(KEY_PART_INFO*, unsigned char const*, unsigned int, bool)
handler::compare_key_in_buffer(unsigned char const*) const
[row_search_end_range_check]                          <-- static, unnamed in trace
row_search_mvcc(...)
ha_innobase::index_read(...)
handler::ha_index_read_map(...)
handler::read_range_first(...)
ha_innobase::read_range_first(...)
handler::multi_range_read_next(...)
handler::ha_multi_range_read_next(...)
IndexRangeScanIterator::Read()
IndexMergeIterator::Init()
```

## Root cause

`handler::compare_key_in_buffer()` (sql/handler.cc) is missing a guard for
multi-value indexes during covering (keyread) scans.

The sibling function `handler::compare_key()` already has this guard:

```cpp
// handler::compare_key() at sql/handler.cc:7476 -- CORRECT
if ((table->key_info[active_index].flags & HA_MULTI_VALUED_KEY) &&
    table->key_read) {
  return -1;  // skip end-range check, let SQL layer filter
}
```

But `handler::compare_key_in_buffer()` has **no such guard**. It calls
`key_cmp()` unconditionally, which calls `Field_typed_array::key_cmp()`,
which calls `val_json()` -> `json_binary::parse_binary()`. During a
covering/keyread scan (as used by IndexMerge), the JSON column data is
**not in the record buffer** -- `parse_binary` reads invalid memory.

### Why MVCC is required to trigger it

The buggy `compare_key_in_buffer()` is called from InnoDB's
`row_search_end_range_check()` (storage/innobase/row/row0sel.cc).
This function is only called at page boundaries when `end_loop >= 100`
(i.e., after iterating through 100+ records in `row_search_mvcc` without
returning a row to the caller).

MVCC-invisible rows (rows inserted by another transaction after the
reader's snapshot) are exactly such "iterated but not returned" records.
Inserting ~2000 rows with the same multi-value key value after a
snapshot ensures that `end_loop` exceeds the threshold when scanning
through those invisible entries in the multi-value index.

### Why the existing InnoDB guard is insufficient

`row_search_mvcc` has a guard at line 4986:

```cpp
!(clust_templ_for_sec && index->is_multi_value())
```

But during a covering scan (IndexMerge sets `keyread=true`):
- `clust_templ_for_sec = (index != clust_index && prebuilt->need_to_access_clustered)`
- `need_to_access_clustered` is **false** (covering scan doesn't need clustered index)
- So `clust_templ_for_sec` is **false** and the guard evaluates to `!(false && true)` = `true`
- The end-range check **proceeds** despite the multi-value index

## Proposed fix

Add the same multi-value guard to `compare_key_in_buffer()` that
`compare_key()` already has:

```cpp
int handler::compare_key_in_buffer(const uchar *buf) const {
  assert(end_range != nullptr && ...);
  assert(range_scan_direction == RANGE_SCAN_ASC);

+ // For multi-valued indexes during index-only scans, key_cmp() calls
+ // Field_typed_array::key_cmp() which needs the virtual column backing
+ // the index. This column is not available during covering scans.
+ // Skip the end-range check and let the SQL layer filter instead.
+ if ((table->key_info[active_index].flags & HA_MULTI_VALUED_KEY) &&
+     table->key_read) {
+   return -1;
+ }

  const ptrdiff_t diff = buf - table->record[0];
  if (diff != 0) move_key_field_offsets(end_range, range_key_part, diff);
  ...
```

Returning `-1` means "key is within range" -- InnoDB continues returning
rows to the SQL layer, which applies the real WHERE filter. This is safe
and correct; the end-range check is purely a performance optimization.

## Workaround

Avoid the IndexMerge plan on multi-value indexes:

```sql
SELECT /*+ NO_INDEX_MERGE(calendar_events) */ ...
FROM calendar_events
WHERE (inquiry_id = ? OR json_contains(groupclients, ?)) ...
```

Or rewrite the `OR` as `UNION`.
