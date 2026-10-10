import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from pymongo import MongoClient
import app.main as app_main


class TestExplainEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.test_db = cls.client["test_attendance_db_explain_unit"]
        app_main.db = cls.test_db
        app_main.init_indexes(cls.test_db)
        cls.tc = TestClient(app_main.app)

        # Seed realistic dataset so MongoDB planner reliably selects indexes
        cls.test_db.employees.delete_many({})
        cls.test_db.attendance_logs.delete_many({})

        cls.test_db.employees.insert_many([
            {
                "emp_code": f"EMP{i:04d}",
                "name": f"Employee {i}",
                "email": f"emp{i}@example.com",
                "department": "Engineering" if i <= 10 else "Support",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
                "created_at": datetime.now(timezone.utc),
            }
            for i in range(1, 21)
        ])

        logs = []
        for i in range(1, 21):
            for d in range(1, 25):
                logs.append({
                    "emp_code": f"EMP{i:04d}",
                    "date": f"2026-07-{d:02d}",
                    "status": "PRESENT",
                    "late_minutes": 15 if d % 3 == 0 else 0,
                    "work_hours": 8.5,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                })
        cls.test_db.attendance_logs.insert_many(logs)

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database("test_attendance_db_explain_unit")
        cls.client.close()

    def test_explain_attendance_list(self):
        res = self.tc.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-20")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "attendance_list")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        explain_str = str(data["explain"])
        self.assertIn("IXSCAN", explain_str)
        self.assertNotIn("COLLSCAN", explain_str)

    def test_explain_employee_monthly(self):
        res = self.tc.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "employee_monthly")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        explain_str = str(data["explain"])
        self.assertIn("IXSCAN", explain_str)
        self.assertNotIn("COLLSCAN", explain_str)

    def test_explain_department_summary(self):
        res = self.tc.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "department_summary")
        self.assertEqual(data["collection"], "employees")
        self.assertIn("explain", data)
        explain_str = str(data["explain"])
        self.assertIn("IXSCAN", explain_str)
        self.assertNotIn("COLLSCAN", explain_str)

    def test_explain_late_leaderboard(self):
        res = self.tc.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "late_leaderboard")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        explain_str = str(data["explain"])
        self.assertIn("IXSCAN", explain_str)
        self.assertNotIn("COLLSCAN", explain_str)

    def test_explain_department_trend(self):
        res = self.tc.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-20")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "department_trend")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)
        explain_str = str(data["explain"])
        self.assertIn("IXSCAN", explain_str)
        self.assertNotIn("COLLSCAN", explain_str)

    def test_explain_unsupported_and_missing_params(self):
        # Unsupported endpoint
        res_bad = self.tc.get("/admin/explain/unknown_query")
        self.assertEqual(res_bad.status_code, 422)

        # Missing emp_code for employee_monthly
        res_no_emp = self.tc.get("/admin/explain/employee_monthly?month=2026-07")
        self.assertEqual(res_no_emp.status_code, 422)

        # Missing month for employee_monthly
        res_no_m = self.tc.get("/admin/explain/employee_monthly?emp_code=EMP0001")
        self.assertEqual(res_no_m.status_code, 422)

        # Missing month for department_summary
        res_no_m_dept = self.tc.get("/admin/explain/department_summary")
        self.assertEqual(res_no_m_dept.status_code, 422)

        # Missing month for late_leaderboard
        res_no_m_lb = self.tc.get("/admin/explain/late_leaderboard")
        self.assertEqual(res_no_m_lb.status_code, 422)

        # Missing department / from / to for department_trend
        res_no_dept = self.tc.get("/admin/explain/department_trend?from=2026-07-01&to=2026-07-10")
        self.assertEqual(res_no_dept.status_code, 422)

        res_no_from = self.tc.get("/admin/explain/department_trend?department=Engineering&to=2026-07-10")
        self.assertEqual(res_no_from.status_code, 422)

        # to < from
        res_bad_dates = self.tc.get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-01")
        self.assertEqual(res_bad_dates.status_code, 422)

        # range > 92 days
        res_range = self.tc.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-04-05")
        self.assertEqual(res_range.status_code, 422)


# ---------------------------------------------------------------------------
# Query-plan regression tests for GET /attendance index selection
# ---------------------------------------------------------------------------
# These tests verify that the query planner selects efficient indexes for each
# filter combination and that no unexpected SORT stage appears.  They use
# explain directly against the test database (20 employees x 24 days = 480
# attendance_log rows) so keysExamined bounds are tight and reproducible.
#
# Key invariants measured on 100,000 rows and documented in DECISIONS.md:
#
#   Filter pattern              Winning index          keysExam    SORT?
#   --------------------------  ---------------------  ----------  -----
#   no filters                  date_emp               page_size   no
#   emp_code only               emp_date_uniq (bwd)    page_size   no
#   emp_code + date range       emp_date_uniq (bwd)    range_size  no
#   date range + status         date_emp               range_size  no
# ---------------------------------------------------------------------------

class TestAttendanceListQueryPlan(unittest.TestCase):
    """Regression tests for GET /attendance index selection."""

    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    DB_NAME   = "test_attendance_db_qplan_regression"

    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(cls.MONGO_URI)
        cls.db = cls.client[cls.DB_NAME]
        app_main.db = cls.db
        app_main.init_indexes(cls.db)
        cls.col = cls.db.attendance_logs

        cls.db.employees.delete_many({})
        cls.db.attendance_logs.delete_many({})

        # 20 employees x 24 days = 480 rows
        cls.db.employees.insert_many([
            {
                "emp_code": f"Q{i:03d}",
                "name": f"Employee {i}",
                "email": f"q{i}@example.com",
                "department": "Eng" if i <= 10 else "Ops",
                "shift_start": "09:00", "shift_end": "18:00",
                "joined_on": "2026-01-01",
                "created_at": datetime.now(timezone.utc),
            }
            for i in range(1, 21)
        ])
        logs = []
        for i in range(1, 21):
            for d in range(1, 25):
                logs.append({
                    "emp_code": f"Q{i:03d}",
                    "date": f"2026-07-{d:02d}",
                    "status": "PRESENT" if d % 5 != 0 else "ABSENT",
                    "late_minutes": 20 if d % 4 == 0 else 0,
                    "work_hours": 9.0, "overtime_minutes": 0,
                    "half_day": False, "history": [],
                })
        cls.db.attendance_logs.insert_many(logs)

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database(cls.DB_NAME)
        cls.client.close()

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _winning_index(plan):
        wp = plan.get("queryPlanner", {}).get("winningPlan", {})
        def scan(stage):
            if stage is None:
                return None
            if stage.get("stage") == "IXSCAN":
                return stage.get("indexName", "")
            r = scan(stage.get("inputStage"))
            if r:
                return r
            for s in stage.get("inputStages", []):
                r = scan(s)
                if r:
                    return r
            return None
        return scan(wp) or "none"

    @staticmethod
    def _has_sort(plan):
        wp = plan.get("queryPlanner", {}).get("winningPlan", {})
        def walk(stage):
            if stage is None:
                return False
            if stage.get("stage") == "SORT":
                return True
            if walk(stage.get("inputStage")):
                return True
            return any(walk(s) for s in stage.get("inputStages", []))
        return walk(wp)

    def _explain(self, filter_doc, page_size=20):
        cmd = {
            "find": "attendance_logs",
            "filter": filter_doc,
            "sort": {"date": -1, "emp_code": 1},
            "limit": page_size,
        }
        return self.db.command("explain", cmd, verbosity="executionStats")

    # ---- scenario 1: no filters -------------------------------------------

    def test_no_filter_uses_date_emp_index_no_sort(self):
        plan = self._explain({})
        es   = plan["executionStats"]
        idx  = self._winning_index(plan)
        self.assertIn("date_-1", idx,
            f"Expected date-descending index for no-filter query, got: {idx!r}")
        self.assertFalse(self._has_sort(plan),
            "No-filter query must not have an in-memory SORT stage")
        # Should examine no more keys than it returns (index-prefix pagination)
        self.assertLessEqual(
            es["totalKeysExamined"], es["nReturned"] * 2,
            f"keysExam={es['totalKeysExamined']} nRet={es['nReturned']}")

    # ---- scenario 2: emp_code only ----------------------------------------

    def test_emp_code_filter_uses_emp_date_index_no_sort(self):
        plan = self._explain({"emp_code": "Q001"})
        es   = plan["executionStats"]
        idx  = self._winning_index(plan)
        # emp_date index: auto-named 'emp_code_1_date_1' or custom 'emp_date_uniq'
        self.assertTrue(
            "emp_code_1" in idx or "emp_date" in idx,
            f"Expected emp_code-leading index for emp_code-only query, got: {idx!r}")
        self.assertFalse(self._has_sort(plan),
            "emp_code-only query must not have an in-memory SORT stage")
        # Efficiency: must not examine many more keys than returned
        self.assertLessEqual(
            es["totalKeysExamined"], es["nReturned"] * 3,
            f"emp_code-only query examined {es['totalKeysExamined']} keys "
            f"for {es['nReturned']} docs — wasteful index selection")

    # ---- scenario 3: emp_code + date range (narrow) -----------------------

    def test_emp_code_and_date_range_efficient_no_sort(self):
        filter_doc = {
            "emp_code": "Q001",
            "date": {"$gte": "2026-07-01", "$lte": "2026-07-10"},
        }
        plan = self._explain(filter_doc)
        es   = plan["executionStats"]
        idx  = self._winning_index(plan)
        self.assertTrue(
            "emp_code_1" in idx or "emp_date" in idx,
            f"Expected emp_code-leading index for emp_code+date query, got: {idx!r}")
        self.assertFalse(self._has_sort(plan),
            "emp_code+date-range query must not have an in-memory SORT stage")
        # 10-day range for 1 employee = at most 10 keys; allow small overhead
        self.assertLessEqual(
            es["totalKeysExamined"], 30,
            f"emp_code+date range examined {es['totalKeysExamined']} keys — too many")

    # ---- scenario 4: date range + status (no emp_code) -------------------

    def test_date_range_status_filter_no_collscan_no_sort(self):
        filter_doc = {
            "date": {"$gte": "2026-07-01", "$lte": "2026-07-07"},
            "status": "PRESENT",
        }
        plan = self._explain(filter_doc)
        es   = plan["executionStats"]
        idx  = self._winning_index(plan)
        self.assertNotEqual(idx, "none",
            "date+status query must use an index, not a COLLSCAN")
        self.assertFalse(self._has_sort(plan),
            "date+status query must not have an in-memory SORT stage")
        # 7 days x 20 employees = 140 rows; allow generous overhead for status post-filter
        self.assertLessEqual(
            es["totalKeysExamined"], 300,
            f"date+status examined {es['totalKeysExamined']} keys — suspiciously high")

    # ---- correctness: verify returned sort order --------------------------

    def test_results_date_desc_emp_code_filter(self):
        """Actual document order is date DESC when emp_code is filtered."""
        docs = list(
            self.col.find({"emp_code": "Q001"})
            .sort([("date", -1), ("emp_code", 1)])
            .limit(10)
        )
        dates = [d["date"] for d in docs]
        self.assertEqual(dates, sorted(dates, reverse=True),
            f"Results not in date DESC order: {dates}")

    def test_results_date_desc_no_filter(self):
        """Actual document order is date DESC when no filter is applied."""
        docs = list(
            self.col.find({})
            .sort([("date", -1), ("emp_code", 1)])
            .limit(10)
        )
        dates = [d["date"] for d in docs]
        self.assertEqual(dates, sorted(dates, reverse=True),
            f"Results not in date DESC order: {dates}")

    # ---- parity: explain endpoint must mirror the live query --------------

    def test_explain_endpoint_mirrors_live_query_no_spurious_hint(self):
        """
        The explain endpoint must not inject a hint that the live query omits.
        For a no-filter request, both paths must agree on the winning index.
        """
        tc = TestClient(app_main.app)

        # Live query (no hint, no filter)
        live_plan = self._explain({})
        live_idx  = self._winning_index(live_plan)

        # Explain endpoint (no filters)
        res = tc.get("/admin/explain/attendance_list")
        self.assertEqual(res.status_code, 200)
        explain_str = str(res.json()["explain"])
        self.assertIn("IXSCAN",   explain_str)
        self.assertNotIn("COLLSCAN", explain_str)
        self.assertIn(live_idx, explain_str,
            f"Explain endpoint should report winning index containing "
            f"'{live_idx}' as the live query, but it was not found in output")


if __name__ == "__main__":
    unittest.main()
