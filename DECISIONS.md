# DECISIONS.md

### 1. Indexes
We created the following indexes idempotently on application startup:
* **`employees.emp_code` (unique):** Enforces business key uniqueness across concurrent employee creation requests (Rule R10).
* **`employees.department` & `employees.[department, joined_on]`:** Optimizes department headcount filtering, analytics grouping, and the dynamic headcount lookup in the trend series.
* **`employees.joined_on`:** Accelerates headcount filtering for mid-month and future joiners (Rule R9).
* **`attendance_logs.[emp_code, date]` (unique):** Guarantees exactly one attendance log per employee per calendar day; ensures concurrent punch-ins yield exactly one `201` and conflicts `409`. Serves IXSCAN for employee monthly aggregation, individual attendance history, and department trend matching.
* **`attendance_logs.[date, emp_code]`:** Provides sort-order coverage for `GET /attendance` when no `emp_code` filter is present (date range, status, or no filter at all). In those cases the planner naturally selects this index and produces `date DESC` output without any in-memory SORT stage (keysExamined = nReturned = 20 on 100k rows). When `emp_code` is an equality filter, the planner instead selects the `{emp_code, date}` unique index and traverses it **backward** — this also yields `date DESC` output with no SORT stage and examines only as many keys as it returns. Forcing `{date, emp_code}` unconditionally via a `.hint()` causes 9,501 keys examined vs 20 for an `emp_code`-filtered query — a 475× regression measured on 100,000 records. Therefore **no query hint is applied**; the query planner's natural selection is optimal and correct across all measured filter combinations.
* **`attendance_logs.[date, late_minutes]`:** Directly accelerates `GET /analytics/leaderboard/late` by filtering records with `late_minutes > 0` within the month via index scan.

**Rejected index:** We considered indexing `attendance_logs.status` alone, but rejected it because its low cardinality (5 statuses) provides negligible selectivity compared to the compound index on `(emp_code, date)` and `(date, emp_code)`.

### 2. Punch-in race
When two identical punch-in requests arrive at the exact same instant for the same employee and date:
1. Both requests compute the attendance date (accounting for overnight shift rules).
2. The application checks the database; both might initially find no existing log before commit.
3. Both execute `db.attendance_logs.insert_one(...)`.
4. MongoDB's storage engine evaluates the unique compound index `(emp_code, date)`. Exactly one insert succeeds and commits (returning HTTP `201 Created`).
5. The second insert triggers a MongoDB `DuplicateKeyError`, which the application catches and converts into HTTP `409 Conflict` (`"already punched in for this date"`). No duplicate records can ever be created.

### 3. Ties
When two employees tie on total late minutes at the cutoff of `limit`:
* Standard competition ranking (`$rank` in `$setWindowFields`) assigns both employees the identical rank (e.g. rank 2).
* The limit is applied as a filter stage: `{"$match": {"rank": {"$lte": limit}}}` rather than MongoDB's `$limit`.
* Because both tied employees satisfy `rank <= limit`, **both employees are included in the response**, even if the total row count exceeds the nominal limit.
* Tied rows are deterministically ordered by `total_late_minutes` descending, then `emp_code` ascending.

### 4. Headcount
`GET /analytics/departments/summary` ensures employees with zero logs are counted by:
* Initiating the aggregation pipeline on the `employees` collection (not `attendance_logs`), filtering employees where `joined_on <= end_date`.
* Grouping by `department` to compute `headcount: {"$sum": 1}` and collecting `emp_codes`. This guarantees every employed person contributes to headcount regardless of whether they have attendance records.
* Performing a `$lookup` into `attendance_logs` for those employee codes. If an employee has no logs, the lookup array simply yields no records, leaving headcount intact while reporting `0.0` present days.

### 5. One thing you would change for 100x data
At 100x scale (~10 million attendance records):
* We would implement **monthly pre-aggregated rollups** (e.g. using MongoDB Atlas Triggers, change streams, or a nightly scheduled aggregation pipeline) that store month-level employee and department summary documents.
* While compound indexes ensure `IXSCAN` on 100k records within milliseconds, scanning tens of millions of records for multi-month aggregates would incur significant I/O and working-set memory pressure. Pre-aggregating closed months would make read operations O(1) key-value lookups.
