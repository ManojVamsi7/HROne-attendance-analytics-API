# Employee Attendance & Analytics API

A high-performance FastAPI and MongoDB backend service implementing employee management, attendance lifecycle tracking (punch-in, punch-out, regularization), and advanced analytics with database-level aggregation pipelines.

Built for the HROne Engineering assessment, adhering strictly to the API contract defined in `openapi.yaml` and the data model in `DATA_MODEL.md`.

---

## Features

* **Strict Temporal & Timezone Integrity (R1):**
  * Instant timestamps accepted and returned as integer epoch milliseconds (UTC).
  * Timestamps truncated to whole seconds before calculations and database storage.
  * Attendance dates, shift boundaries, and overnight shifts calculated in IST (UTC+05:30).

* **Automated Derived Metric Calculations (R2–R5):**
  * Grace period tracking (> 10:00 elapsed from shift start) with floored late minutes.
  * Overtime tracking (>= 30 minutes after shift end) across normal and overnight shifts.
  * Exact decimal work-hour calculation using `Decimal` with `ROUND_HALF_UP` (no banker's rounding).
  * Half-day presence detection when work hours are < 4.50.

* **Concurrency & Race-Condition Safety:**
  * Unique compound MongoDB indexes ensure duplicate employee creation and duplicate punch-ins yield HTTP `409 Conflict`.
  * Atomic conditional updates prevent double punch-outs.
  * Optimistic concurrency control on attendance corrections prevents lost history updates.

* **Database-Side Aggregations:**
  * Monthly employee summaries taking into account mid-month joiners (R7).
  * Department summaries including employees with zero attendance records (R9).
  * Leaderboard with standard competition ranking (`1, 2, 2, 4`), cutoff tie inclusion, and orphan record exclusion.
  * Department daily attendance trend with calendar gap filling via MongoDB `$densify` and 7-day moving averages.

* **Performance & Query Explainability:**
  * Startup initialization of all necessary compound indexes.
  * `GET /admin/explain/{endpoint}` returning raw MongoDB execution statistics, verified to use `IXSCAN` and zero `COLLSCAN` on 100,000 records.
  * `GET /attendance` index strategy benchmarked on 100,000 records (500 employees, 200 days):
    * No-filter queries use `{date: -1, emp_code: 1}` and examine a number of keys equal to the page size in the benchmark.
    * Employee-filtered queries use `{emp_code: 1, date: 1}` with backward index traversal, avoiding an in-memory `SORT` stage and examining a number of keys equal to the page size in the benchmark.
    * Date-range and status-filtered queries use the date-oriented index without an in-memory `SORT`; additional keys may be examined because status filtering reduces the results.
    * No index hint is applied. Forcing `{date: -1, emp_code: 1}` unconditionally caused a 475x regression on employee-filtered queries (9,501 keys examined versus 20).

---

## Tech Stack & Prerequisites

* **Python:** Python 3.11+ (tested on Python 3.13)
* **Framework:** FastAPI, Pydantic v2, Uvicorn
* **Database:** MongoDB 6.0+ (requires MongoDB 5.1+ for `$densify` and 5.0+ for `$setWindowFields`; tested on MongoDB 8.2.0)
* **Client Driver:** PyMongo 4.7+

---

## Installation & Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/ManojVamsi7/HROne-attendance-analytics-API.git
   cd HROne-attendance-analytics-API
   ```

2. **Create and activate a virtual environment:**

   ```bash
   # On macOS/Linux:
   python3 -m venv .venv
   source .venv/bin/activate

   # On Windows (PowerShell):
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables:**

   Create a `.env` file in the project root:

   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

   *(Real system environment variables override `.env` values.)*

---

## Running the Application

Start the Uvicorn development server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

* **Interactive API Documentation (Swagger UI):** [http://localhost:8000/docs](http://localhost:8000/docs)
* **Alternative API Documentation (ReDoc):** [http://localhost:8000/redoc](http://localhost:8000/redoc)
* **Health Check:** `GET http://localhost:8000/health`

---

## Running Automated Tests

A comprehensive suite of unit, integration, and concurrency tests is provided in the `tests/` directory. All tests run against isolated temporary test databases and do not mutate your primary database.

Run the test suite using Python's standard `unittest` runner:

```bash
py -3.13 -m unittest discover -s tests
```

To run individual test modules:

```bash
# Core endpoints & CRUD (Phase 2)
py -3.13 tests/test_phase2.py

# Punch-out & regularization (Phase 3)
py -3.13 tests/test_phase3.py

# Analytics endpoints (Phase 4)
py -3.13 tests/test_analytics.py

# Admin explain endpoint & plan checks (Phase 5)
py -3.13 tests/test_explain.py
```

---

## Available Endpoints

### System

* `GET /health` — Readiness probe (MongoDB ping)

### Employees

* `POST /employees` — Create an employee with unique business `emp_code`
* `GET /employees` — Paginated employee list, filterable by department

### Attendance

* `POST /attendance/punch-in` — Record daily punch-in (status `PRESENT`, `WFH`, `ON_DUTY`)
* `POST /attendance/punch-out` — Record punch-out; computes work hours, overtime, and half-day
* `GET /attendance` — Paginated and indexed attendance logs with date and status filters
* `PATCH /attendance/{emp_code}/{date}` — Regularize attendance record and append audit trail

### Analytics

* `GET /analytics/employees/{emp_code}/monthly` — Monthly attendance summary for one employee
* `GET /analytics/departments/summary` — Department attendance breakdown for a month
* `GET /analytics/leaderboard/late` — Late-comers leaderboard with standard competition ranking
* `GET /analytics/departments/{department}/trend` — Daily time series with `$densify` gap filling and 7-day moving average

### Admin

* `GET /admin/explain/{endpoint}` — MongoDB `explain` execution stats for supported endpoints (`attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`)

---

## Limitations & Compatibility Requirements

1. **MongoDB Version:** Requires MongoDB 6.0 or newer. MongoDB 5.1 is the minimum version supporting `$densify`.
2. **Holiday Calendar:** Per assessment Rule R7, working days are defined strictly as Monday through Friday without an external holiday calendar.
3. **Single File Structure:** Per assessment requirements, all core backend application logic is maintained within `app/main.py`.
