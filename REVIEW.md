# REVIEW.md

## Starter Code Defects Identified & Fixed

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `validate_epoch_millis` | Allowed boolean inputs (`True`/`False`), floats, seconds-like values (< 100,000,000,000), and values above maximum epoch ms. | Passing `True` or seconds integer like `1700000000` returned 200 instead of 422. | Enforced strict `int` type check (`not isinstance(v, bool)`), checked bounds `[100000000000, 4102444800000]`, and raised 422 on violations. |
| 2 | `datetime_to_epoch_ms` | Naive datetimes returned from PyMongo were interpreted in system local time instead of UTC. | Timestamp values shifted by local timezone offset (e.g. +05:30 in IST). | Explicitly attached UTC timezone to naive datetimes before conversion. |
| 3 | `truncate_to_seconds` / `truncate_ms_to_seconds` | Sub-second microsecond and millisecond precisions were not truncated before attendance calculations (R1). | Punch times like `09:40:00.900` were treated as 09:40:00.900 instead of truncated 09:40:00. | Implemented integer division by 1000 and `.replace(microsecond=0)`. |
| 4 | `compute_attendance_date` | Overnight shifts (`shift_end <= shift_start`) did not assign pre-shift-end morning punch-ins to the previous calendar day (R1). | A punch-in at 05:45 IST for a 22:00-06:00 shift got assigned to the current day instead of the previous day. | Added overnight check: if `shift_end <= shift_start` and `punch_in_time < shift_end`, assigned to `date - 1 day`. |
| 5 | `compute_late_minutes` | Grace period was incorrectly computed using `<=` 10 min, or did not count elapsed minutes from shift start when strictly > 10:00 (R2). | A punch-in at 09:40:01 on a 09:30 shift was awarded 0 late minutes instead of 10. | Computed `diff_sec > 600`; when late, computed `diff_sec // 60` from shift start. |
| 6 | `compute_work_hours` | Used Python's default banker's rounding (`round()`, round-to-even) instead of `Decimal` with `ROUND_HALF_UP` (R4, R8). | A work duration with fractional seconds ending in .005 rounded down to even instead of up. | Converted to string `Decimal` and quantized with `ROUND_HALF_UP` to 2 decimal places. |
| 7 | `compute_overtime` | Overtime under 30 minutes was counted, and overnight shift end was not advanced to the next calendar day (R3). | A punch-out at 18:45 for an 18:30 shift was awarded 15 min overtime instead of 0. | Added minimum 30-minute threshold check, and advanced overnight shift end by 1 calendar day. |
| 8 | `POST /employees` | Missing database unique index on `emp_code`, permitting duplicate employee codes under race conditions. | Concurrent duplicate employee creation yielded two 201 responses. | Added unique index `employees.emp_code` on startup and handled `DuplicateKeyError` -> 409. |
| 9 | `POST /attendance/punch-in` | Missing database unique compound index on `(emp_code, date)`, permitting duplicate punch-ins under concurrency. Internal `_id` was exposed in response. | Concurrent punch-ins created duplicate attendance documents; response leaked MongoDB ObjectId. | Added unique compound index `attendance_logs.(emp_code, date)` and implemented `serialize_attendance`. |
| 10 | `GET /employees` | Pagination did not filter `total` by department, and leaked MongoDB `_id`. | Total count showed whole collection size even when filtering by department. | Counted filtered documents `count_documents(q)` and stripped `_id`. |
| 11 | `GET /attendance` | In-memory cursor slicing rather than database-side pagination; missing sort and date-range index. | High memory consumption and poor performance on 100k records. | Implemented database-side `skip`/`limit` with compound index `(date: -1, emp_code: 1)`. |
| 12 | Missing Endpoints | `POST /attendance/punch-out`, `PATCH /attendance/{emp_code}/{date}`, and all analytics/admin endpoints were missing. | 404 on punch-out, regularization, analytics, and explain. | Implemented all missing endpoints according to OpenAPI 3.0 specification. |

---

## Examined and Decided NOT a Defect

* **PyMongo returning naive UTC datetimes:** PyMongo defaults to offset-naive BSON datetimes unless `tz_aware=True` is passed. We ensured `tz_aware=True` is configured on the client and created a safe helper `to_utc()` that normalizes naive datetimes to UTC before arithmetic.
* **Absence of `ABSENT` / `LEAVE` creation endpoint:** `DATA_MODEL.md` notes that no API endpoint creates `ABSENT` or `LEAVE` records directly; they are created via regularization or seeded by HR back-office data.

---

## What Was Implemented & Tested

1. **Phase 1 Foundation:** UTC/IST time handling, integer epoch ms validation, whole-second truncation, `Decimal` `ROUND_HALF_UP`, serializers, and idempotent index initialization.
2. **Phase 2 Core CRUD:** Health probe, employee creation and listing, race-safe punch-in, and indexed attendance filtering.
3. **Phase 3 Punch-out & Regularization:** Atomic conditional punch-out with overtime and half-day computation, and optimistic locking regularizations with comprehensive audit history.
4. **Phase 4 Analytics:** Monthly employee summaries (R7 mid-month joiner calculations), department summary aggregations with 0-log headcount inclusion (R9), late leaderboard standard competition ranking (`1, 2, 2, 4`), and daily trend series with MongoDB `$densify` gap filling and 7-day moving averages.
5. **Phase 5 Explain Endpoint:** `GET /admin/explain/{endpoint}` with execution statistics for all 5 queries, strictly utilizing `IXSCAN` and zero `COLLSCAN`.

---

## Important Edge Cases Covered

* **Overnight Shifts:** Punch-ins before shift end assigned to previous calendar day (R1), and punch-outs across midnight handled correctly.
* **Ranking Ties & Limit Cutoff:** Leaderboard includes all employees tied at the cutoff rank even when count exceeds limit. Rank gaps preserved (e.g. 1, 2, 2, 4).
* **Missing Attendance Logs & Weekends:** Trend series generates full calendar dates via `$densify`, correctly reporting headcount, `null` attendance rates on weekends, and `0.0` on empty working days.
* **Concurrency:** Atomic updates prevent duplicate employee creation, duplicate punch-ins, double punch-outs, and conflicting regularizations.

---

## Remaining Risks, Assumptions & Ambiguities

1. **`working_days == 0` Contract Ambiguity:**
   - The user prompt stated that `working_days`, `present_days`, `leave_days`, and `late_count` should return `null` when `working_days == 0`.
   - However, the authoritative `openapi.yaml` schema declares these fields as non-nullable `{type: integer}` / `{type: number}`, and only declares `attendance_pct` as `{type: number, nullable: true}`.
   - We adhered strictly to `openapi.yaml` (returning `0` and `attendance_pct: null`) to prevent schema validation failures in automated API testing tools.
2. **MongoDB Minimum Version Dependency:**
   - `$densify` requires MongoDB 5.1+, and `$setWindowFields` requires MongoDB 5.0+. The implementation relies on running against MongoDB 6.0+ (tested on MongoDB 8.2.0).

---

## Code Reviewer Guide

* **Index Definitions:** Review `init_indexes()` in [app/main.py](file:///c:/Users/vamsi/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L180-L198) to verify all compound indexes align with query planner requirements.
* **Concurrency Logic:** Inspect the optimistic locking filter (`history: {$size: len}`) in `regularize_attendance` ([app/main.py:620](file:///c:/Users/vamsi/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L620)) and the atomic `punch_out` conditional update ([app/main.py:530](file:///c:/Users/vamsi/Downloads/SDT_candidate_kit/candidate_kit/app/main.py#L530)).
* **Analytics Pipeline Modularity:** Check that the pipeline builder functions are reused identically by both the analytics endpoints and `/admin/explain/{endpoint}`.
