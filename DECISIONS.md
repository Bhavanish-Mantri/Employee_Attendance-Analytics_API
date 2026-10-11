# DECISIONS.md (about 150-200 words in total - plain language, your own words)

1. **Indexes.** On `employees`, we created unique `(emp_code)` to guarantee business key uniqueness, and `(department, emp_code)` to back department filtering with sorted pagination. On `attendance_logs`, we created compound unique `(emp_code, date)` to atomically prevent duplicate daily records, `(emp_code, punch_in)` to support instant open-record lookups for punch-out, and `(date, emp_code)` to allow index-covered sorting for `/attendance`. We considered and rejected an index on `date` alone because the compound `(date, emp_code)` index already satisfies equality and range scans on `date` while providing the required tie-breaking sort.

2. **Punch-in race.** When two identical punch-in requests arrive simultaneously, both compute the target attendance date. Because `attendance_logs` enforces a compound unique index on `(emp_code, date)`, MongoDB atomically accepts the first `insert_one` and immediately rejects the second with a `DuplicateKeyError`. Our code catches this exception and returns HTTP 409, ensuring exactly one 201 and race safety without check-then-insert windows.

3. **Ties.** Our leaderboard pipeline calculates standard competition ranking using MongoDB's `$rank` window function (`1, 2, 2, 4`), sorting rows by late minutes descending and `emp_code` ascending. The `limit` filter is evaluated on the computed `rank` (`rank <= limit`), ensuring that if multiple employees tie at the cutoff rank, all tied employees are returned.

4. **Headcount.** The department summary aggregation pipeline drives from the `employees` collection filtered by `joined_on <= month_end`, and joins `attendance_logs` via `$lookup`. By grouping from the employee collection, employees who have zero attendance records in that month are preserved and counted in departmental headcount.

5. **One thing you would change for 100x data.** At 100x scale (10M+ records), we would maintain pre-aggregated daily and monthly departmental rollups incrementally using MongoDB Change Streams or scheduled `$merge` pipelines, avoiding repeated cross-collection aggregations over raw log records.
