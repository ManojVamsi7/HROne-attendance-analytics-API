import os
import sys
import unittest
from datetime import datetime, date, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient
from fastapi.testclient import TestClient

# Use an isolated test database
os.environ["MONGO_DB"] = "test_attendance_db_analytics_phase4"

from app.main import app, db, init_indexes, round_half_up

client = TestClient(app)


class TestAnalyticsEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Clean test database and create indexes
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})
        init_indexes(db)

    @classmethod
    def tearDownClass(cls):
        # Drop test database on completion
        mongo_cli = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
        mongo_cli.drop_database("test_attendance_db_analytics_phase4")
        mongo_cli.close()

    def setUp(self):
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})

    # ----------------------------------------------------------------------- #
    # 1. Monthly Employee Analytics Tests
    # ----------------------------------------------------------------------- #
    def test_employee_monthly_normal_and_rules(self):
        # EMP0001 joined 2026-07-06 (Monday). In July 2026, month is 2026-07-01 to 2026-07-31.
        # July 1, 2, 3 are Wed, Thu, Fri (3 days).
        # July 2026 has 23 total weekdays.
        # EMP0001 joined on July 6 (Monday), so working days = 23 - 3 = 20 days.
        db.employees.insert_one({
            "emp_code": "EMP0001",
            "name": "Alice Smith",
            "email": "alice@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-06",
            "created_at": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        })

        # Logs for EMP0001:
        # 1. 2026-07-06 (Mon): PRESENT, full day (work_hours=9.0, late=15, overtime=40)
        # 2. 2026-07-07 (Tue): WFH, half day (work_hours=4.0, half_day=True) -> 0.5 present
        # 3. 2026-07-08 (Wed): ON_DUTY, full day -> 1.0 present
        # 4. 2026-07-09 (Thu): LEAVE -> leave_days=1, 0 present
        # 5. 2026-07-10 (Fri): ABSENT -> 0 present
        # 6. 2026-07-11 (Sat, weekend): PRESENT, late=20, overtime=60 -> weekend does NOT count as present_days, but adds late & overtime
        db.attendance_logs.insert_many([
            {
                "emp_code": "EMP0001",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 6, 4, 15, 0, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc),
                "work_hours": 9.0,
                "late_minutes": 15,
                "overtime_minutes": 40,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-07",
                "status": "WFH",
                "punch_in": datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 7, 8, 0, 0, tzinfo=timezone.utc),
                "work_hours": 4.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": True,
                "history": [],
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-08",
                "status": "ON_DUTY",
                "punch_in": datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 8, 13, 0, 0, tzinfo=timezone.utc),
                "work_hours": 9.0,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-09",
                "status": "LEAVE",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-10",
                "status": "ABSENT",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [],
            },
            {
                "emp_code": "EMP0001",
                "date": "2026-07-11",
                "status": "PRESENT",
                "punch_in": datetime(2026, 7, 11, 4, 20, 0, tzinfo=timezone.utc),
                "punch_out": datetime(2026, 7, 11, 14, 0, 0, tzinfo=timezone.utc),
                "work_hours": 9.0,
                "late_minutes": 20,
                "overtime_minutes": 60,
                "half_day": False,
                "history": [],
            },
        ])

        res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertEqual(data["month"], "2026-07")
        self.assertEqual(data["working_days"], 20)
        # present_days = 1.0 (PRESENT) + 0.5 (WFH half) + 1.0 (ON_DUTY) = 2.5
        # Saturday record is NOT counted towards present_days
        self.assertEqual(data["present_days"], 2.5)
        self.assertEqual(data["leave_days"], 1)
        # late_count: 2 (July 6 has 15 min, July 11 Sat has 20 min)
        self.assertEqual(data["late_count"], 2)
        self.assertEqual(data["total_late_minutes"], 35)
        self.assertEqual(data["total_overtime_minutes"], 100)
        # attendance_pct: 2.5 / 20 * 100 = 12.5
        self.assertEqual(data["attendance_pct"], 12.5)

    def test_employee_monthly_future_joiner_and_no_logs(self):
        # EMP0002 joined on 2026-08-01. July 2026 working_days should be 0, attendance_pct should be None.
        db.employees.insert_one({
            "emp_code": "EMP0002",
            "name": "Bob Future",
            "email": "bob@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-08-01",
            "created_at": datetime(2026, 7, 1, 4, 0, 0, tzinfo=timezone.utc),
        })

        res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["working_days"], 0)
        self.assertIsNone(data["attendance_pct"])
        self.assertEqual(data["present_days"], 0.0)

    def test_employee_monthly_unknown_and_validation(self):
        # 404 for unknown employee
        res = client.get("/analytics/employees/UNKNOWN99/monthly?month=2026-07")
        self.assertEqual(res.status_code, 404)
        self.assertIn("detail", res.json())

        # 422 for invalid month formats
        res2 = client.get("/analytics/employees/EMP0001/monthly?month=2026-13")
        self.assertEqual(res2.status_code, 422)
        res3 = client.get("/analytics/employees/EMP0001/monthly?month=bad-format")
        self.assertEqual(res3.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 2. Department Summary Tests
    # ----------------------------------------------------------------------- #
    def test_department_summary(self):
        # 3 employees:
        # E1: Engineering, joined 2026-01-01 (has logs)
        # E2: Engineering, joined 2026-07-15 (0 logs, must be counted in headcount)
        # E3: Engineering, joined 2026-08-01 (future joiner, excluded from July headcount)
        # E4: HR, joined 2026-01-01 (has 1 log)
        # E5: Design, joined 2026-08-05 (only employee in Design, so Design headcount=0 and omitted)
        db.employees.insert_many([
            {"emp_code": "E1", "name": "E1", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
            {"emp_code": "E2", "name": "E2", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-15", "created_at": datetime.now(timezone.utc)},
            {"emp_code": "E3", "name": "E3", "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01", "created_at": datetime.now(timezone.utc)},
            {"emp_code": "E4", "name": "E4", "department": "HR", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime.now(timezone.utc)},
            {"emp_code": "E5", "name": "E5", "department": "Design", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-05", "created_at": datetime.now(timezone.utc)},
        ])

        # Logs in July:
        # E1:
        # - 2026-07-06 (Mon): PRESENT, work_hours=8.0, late=10
        # - 2026-07-07 (Tue): ON_DUTY, work_hours=10.0, late=0 -> on_duty_count=1
        # - 2026-07-08 (Wed): LEAVE -> leave_count=1
        # E4 (HR):
        # - 2026-07-06 (Mon): PRESENT, work_hours=7.5, late=0
        db.attendance_logs.insert_many([
            {
                "emp_code": "E1",
                "date": "2026-07-06",
                "status": "PRESENT",
                "work_hours": 8.0,
                "late_minutes": 10,
                "half_day": False,
            },
            {
                "emp_code": "E1",
                "date": "2026-07-07",
                "status": "ON_DUTY",
                "work_hours": 10.0,
                "late_minutes": 0,
                "half_day": False,
            },
            {
                "emp_code": "E1",
                "date": "2026-07-08",
                "status": "LEAVE",
                "work_hours": None,
                "late_minutes": 0,
                "half_day": False,
            },
            {
                "emp_code": "E4",
                "date": "2026-07-06",
                "status": "PRESENT",
                "work_hours": 7.5,
                "late_minutes": 0,
                "half_day": False,
            },
        ])

        res = client.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["month"], "2026-07")
        items = data["items"]
        # Expected departments: Engineering and HR. Design has 0 headcount in July, so omitted.
        self.assertEqual(len(items), 2)
        depts = {item["department"]: item for item in items}
        self.assertIn("Engineering", depts)
        self.assertIn("HR", depts)
        self.assertNotIn("Design", depts)

        # Check Engineering:
        eng = depts["Engineering"]
        # E1 and E2 joined <= 2026-07-31. E3 joined 2026-08-01. Headcount = 2
        self.assertEqual(eng["headcount"], 2)
        # present_days = 1.0 (PRESENT) + 1.0 (ON_DUTY) = 2.0
        self.assertEqual(eng["present_days"], 2.0)
        # avg_work_hours = (8.0 + 10.0) / 2 = 9.0
        self.assertEqual(eng["avg_work_hours"], 9.0)
        self.assertEqual(eng["late_count"], 1)
        self.assertEqual(eng["total_late_minutes"], 10)
        self.assertEqual(eng["leave_count"], 1)
        self.assertEqual(eng["on_duty_count"], 1)

        # Department filter check:
        res_filtered = client.get("/analytics/departments/summary?month=2026-07&department=HR")
        self.assertEqual(res_filtered.status_code, 200)
        items_hr = res_filtered.json()["items"]
        self.assertEqual(len(items_hr), 1)
        self.assertEqual(items_hr[0]["department"], "HR")

    # ----------------------------------------------------------------------- #
    # 3. Late Leaderboard Tests
    # ----------------------------------------------------------------------- #
    def test_late_leaderboard_competition_ranking_and_ties(self):
        # Employees:
        # EMP1 (Eng), EMP2 (Eng), EMP3 (Eng), EMP4 (Sales), ORPHAN (not in employees collection)
        db.employees.insert_many([
            {"emp_code": "EMP1", "name": "Emp One", "department": "Engineering"},
            {"emp_code": "EMP2", "name": "Emp Two", "department": "Engineering"},
            {"emp_code": "EMP3", "name": "Emp Three", "department": "Engineering"},
            {"emp_code": "EMP4", "name": "Emp Four", "department": "Sales"},
            {"emp_code": "EMP5", "name": "Emp Five", "department": "Engineering"},
        ])

        # Late logs for July 2026:
        # EMP1: 50 min late
        # EMP2: 30 min late
        # EMP3: 30 min late (tied with EMP2)
        # EMP5: 10 min late
        # ORPHAN: 100 min late (must be ignored)
        # EMP4: 40 min late (in Sales)
        db.attendance_logs.insert_many([
            {"emp_code": "EMP1", "date": "2026-07-06", "late_minutes": 50},
            {"emp_code": "EMP2", "date": "2026-07-06", "late_minutes": 30},
            {"emp_code": "EMP3", "date": "2026-07-06", "late_minutes": 30},
            {"emp_code": "EMP5", "date": "2026-07-06", "late_minutes": 10},
            {"emp_code": "ORPHAN", "date": "2026-07-06", "late_minutes": 100},
            {"emp_code": "EMP4", "date": "2026-07-06", "late_minutes": 40},
        ])

        # Global leaderboard with limit=2:
        # Total late minutes order:
        # EMP1: 50 min -> Rank 1
        # EMP4: 40 min -> Rank 2
        # EMP2: 30 min -> Rank 3
        # EMP3: 30 min -> Rank 3
        # EMP5: 10 min -> Rank 5
        # Limit=2 returns Rank 1 and Rank 2. Total 2 items.
        res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["emp_code"], "EMP1")
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[1]["emp_code"], "EMP4")
        self.assertEqual(items[1]["rank"], 2)

        # Test Department Filter & Cutoff Ties:
        # Filter department=Engineering with limit=2:
        # EMP1: 50 min -> Rank 1
        # EMP2: 30 min -> Rank 2
        # EMP3: 30 min -> Rank 2 (tied with EMP2 at cutoff rank 2!)
        # EMP5: 10 min -> Rank 4 (skipped rank 3!)
        # Since limit=2 and EMP2 & EMP3 both have rank 2 <= limit:
        # Both must be included! Total rows returned = 3 (EMP1, EMP2, EMP3).
        # Tied rows sorted by emp_code ascending: EMP2 then EMP3.
        res_dept = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering&limit=2")
        self.assertEqual(res_dept.status_code, 200)
        dept_items = res_dept.json()["items"]
        self.assertEqual(len(dept_items), 3)
        self.assertEqual(dept_items[0]["emp_code"], "EMP1")
        self.assertEqual(dept_items[0]["rank"], 1)
        self.assertEqual(dept_items[1]["emp_code"], "EMP2")
        self.assertEqual(dept_items[1]["rank"], 2)
        self.assertEqual(dept_items[2]["emp_code"], "EMP3")
        self.assertEqual(dept_items[2]["rank"], 2)

        # Check full engineering ranking to verify rank skip (1, 2, 2, 4):
        res_full = client.get("/analytics/leaderboard/late?month=2026-07&department=Engineering&limit=10")
        full_items = res_full.json()["items"]
        self.assertEqual(len(full_items), 4)
        self.assertEqual(full_items[3]["emp_code"], "EMP5")
        self.assertEqual(full_items[3]["rank"], 4)  # Rank 3 was skipped!

        # Check limit boundary validation (1 to 50):
        res_bad_low = client.get("/analytics/leaderboard/late?month=2026-07&limit=0")
        self.assertEqual(res_bad_low.status_code, 422)
        res_bad_high = client.get("/analytics/leaderboard/late?month=2026-07&limit=51")
        self.assertEqual(res_bad_high.status_code, 422)

    # ----------------------------------------------------------------------- #
    # 4. Department Trend Tests ($densify, Headcount, Moving Avg)
    # ----------------------------------------------------------------------- #
    def test_department_trend(self):
        # Department Engineering with 2 employees:
        # EMP_A: joined 2026-07-06 (Mon)
        # EMP_B: joined 2026-07-08 (Wed)
        db.employees.insert_many([
            {"emp_code": "EMP_A", "name": "Emp A", "department": "Engineering", "joined_on": "2026-07-06"},
            {"emp_code": "EMP_B", "name": "Emp B", "department": "Engineering", "joined_on": "2026-07-08"},
        ])

        # Logs:
        # 2026-07-06 (Mon): EMP_A is PRESENT full day -> present_count = 1.0, headcount = 1, rate = 1.0
        # 2026-07-07 (Tue): EMP_A is WFH half day (half_day=True) -> present_count = 0.5, headcount = 1, rate = 0.5
        # 2026-07-08 (Wed): EMP_A is PRESENT, EMP_B is PRESENT -> present_count = 2.0, headcount = 2, rate = 1.0
        # 2026-07-09 (Thu): No records at all! -> present_count = 0.0, headcount = 2, rate = 0.0
        # 2026-07-10 (Fri): EMP_A is PRESENT, late=15 -> present_count = 1.0, late_count = 1, headcount = 2, rate = 0.5
        # 2026-07-11 (Sat, weekend): EMP_A worked (PRESENT) -> present_count = 1.0, is_working_day = False, rate = null
        # 2026-07-12 (Sun, weekend): No records -> present_count = 0.0, is_working_day = False, rate = null
        db.attendance_logs.insert_many([
            {"emp_code": "EMP_A", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0},
            {"emp_code": "EMP_A", "date": "2026-07-07", "status": "WFH", "half_day": True, "late_minutes": 0},
            {"emp_code": "EMP_A", "date": "2026-07-08", "status": "PRESENT", "half_day": False, "late_minutes": 0},
            {"emp_code": "EMP_B", "date": "2026-07-08", "status": "PRESENT", "half_day": False, "late_minutes": 0},
            {"emp_code": "EMP_A", "date": "2026-07-10", "status": "PRESENT", "half_day": False, "late_minutes": 15},
            {"emp_code": "EMP_A", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        ])

        # Query range from 2026-07-05 (Sun) to 2026-07-12 (Sun) -> 8 calendar days
        res = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-12")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["department"], "Engineering")
        items = data["items"]
        # Exactly one row per calendar day guaranteed by $densify
        self.assertEqual(len(items), 8)

        # Day 1: 2026-07-05 (Sun):
        # EMP_A joins on 07-06, EMP_B joins on 07-08. Headcount on 07-05 = 0. Weekend.
        d0 = items[0]
        self.assertEqual(d0["date"], "2026-07-05")
        self.assertFalse(d0["is_working_day"])
        self.assertEqual(d0["headcount"], 0)
        self.assertEqual(d0["present_count"], 0.0)
        self.assertIsNone(d0["attendance_rate"])
        self.assertIsNone(d0["moving_avg_7d"])

        # Day 2: 2026-07-06 (Mon):
        # EMP_A joined. Headcount = 1. present_count = 1.0. rate = 1.0.
        # Window: [None, 1.0] -> non-null rate = [1.0] -> moving_avg = 1.0
        d1 = items[1]
        self.assertEqual(d1["date"], "2026-07-06")
        self.assertTrue(d1["is_working_day"])
        self.assertEqual(d1["headcount"], 1)
        self.assertEqual(d1["present_count"], 1.0)
        self.assertEqual(d1["attendance_rate"], 1.0)
        self.assertEqual(d1["moving_avg_7d"], 1.0)

        # Day 3: 2026-07-07 (Tue):
        # Headcount = 1. EMP_A half day -> present_count = 0.5. rate = 0.5.
        # Window: [None, 1.0, 0.5] -> non-null [1.0, 0.5] -> moving_avg = (1.0 + 0.5)/2 = 0.75
        d2 = items[2]
        self.assertEqual(d2["date"], "2026-07-07")
        self.assertTrue(d2["is_working_day"])
        self.assertEqual(d2["headcount"], 1)
        self.assertEqual(d2["present_count"], 0.5)
        self.assertEqual(d2["attendance_rate"], 0.5)
        self.assertEqual(d2["moving_avg_7d"], 0.75)

        # Day 4: 2026-07-08 (Wed):
        # EMP_B joined. Headcount = 2. present_count = 2.0. rate = 1.0.
        # Window: [None, 1.0, 0.5, 1.0] -> non-null [1.0, 0.5, 1.0] -> moving_avg = 2.5 / 3 = 0.8333
        d3 = items[3]
        self.assertEqual(d3["date"], "2026-07-08")
        self.assertTrue(d3["is_working_day"])
        self.assertEqual(d3["headcount"], 2)
        self.assertEqual(d3["present_count"], 2.0)
        self.assertEqual(d3["attendance_rate"], 1.0)
        self.assertEqual(d3["moving_avg_7d"], 0.8333)

        # Day 5: 2026-07-09 (Thu):
        # Zero logs densified! Headcount = 2. rate = 0.0.
        # Window: [None, 1.0, 0.5, 1.0, 0.0] -> (1.0 + 0.5 + 1.0 + 0.0)/4 = 2.5/4 = 0.625
        d4 = items[4]
        self.assertEqual(d4["date"], "2026-07-09")
        self.assertTrue(d4["is_working_day"])
        self.assertEqual(d4["headcount"], 2)
        self.assertEqual(d4["present_count"], 0.0)
        self.assertEqual(d4["attendance_rate"], 0.0)
        self.assertEqual(d4["moving_avg_7d"], 0.625)

        # Day 6: 2026-07-10 (Fri):
        # 1 present, late_count = 1. rate = 1/2 = 0.5.
        # Window non-null: [1.0, 0.5, 1.0, 0.0, 0.5] -> sum 3.0 / 5 = 0.6
        d5 = items[5]
        self.assertEqual(d5["date"], "2026-07-10")
        self.assertEqual(d5["late_count"], 1)
        self.assertEqual(d5["attendance_rate"], 0.5)
        self.assertEqual(d5["moving_avg_7d"], 0.6)

        # Day 7: 2026-07-11 (Sat, weekend):
        # Weekend, is_working_day = False. present_count = 1.0. attendance_rate = null.
        # Moving avg: weekend rate is null, so excluded. Still 0.6!
        d6 = items[6]
        self.assertEqual(d6["date"], "2026-07-11")
        self.assertFalse(d6["is_working_day"])
        self.assertEqual(d6["present_count"], 1.0)
        self.assertIsNone(d6["attendance_rate"])
        self.assertEqual(d6["moving_avg_7d"], 0.6)

    def test_department_trend_errors(self):
        # 404 for unknown department
        res404 = client.get("/analytics/departments/NonExistent/trend?from=2026-07-01&to=2026-07-05")
        self.assertEqual(res404.status_code, 404)
        self.assertIn("detail", res404.json())

        # Insert 1 employee in Sales so department exists
        db.employees.insert_one({"emp_code": "S1", "department": "Sales", "joined_on": "2026-01-01"})

        # 422 if to < from
        res_to_less = client.get("/analytics/departments/Sales/trend?from=2026-07-10&to=2026-07-05")
        self.assertEqual(res_to_less.status_code, 422)

        # 422 if range > 92 days
        # 2026-01-01 to 2026-04-03 is 93 days
        res_too_long = client.get("/analytics/departments/Sales/trend?from=2026-01-01&to=2026-04-03")
        self.assertEqual(res_too_long.status_code, 422)

        # Exactly 92 days must succeed (2026-01-01 to 2026-04-02 is 92 days)
        res_92 = client.get("/analytics/departments/Sales/trend?from=2026-01-01&to=2026-04-02")
        self.assertEqual(res_92.status_code, 200)
        self.assertEqual(len(res_92.json()["items"]), 92)

    def test_department_trend_completely_empty_logs(self):
        # Known department with employees, but zero attendance logs during the entire requested date range
        db.employees.insert_one({
            "emp_code": "SUP001",
            "name": "Sam Support",
            "department": "CustomerSupport",
            "joined_on": "2026-07-06"  # Monday
        })
        # Insert a log for a DIFFERENT department to verify cross-department isolation
        db.employees.insert_one({
            "emp_code": "ENG999",
            "name": "Eve Engineer",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })
        db.attendance_logs.insert_one({
            "emp_code": "ENG999",
            "date": "2026-07-06",
            "status": "PRESENT",
            "work_hours": 8.0,
            "late_minutes": 0,
            "half_day": False
        })

        # Request CustomerSupport from 2026-07-05 (Sun) to 2026-07-08 (Wed) -> 4 calendar days
        res = client.get("/analytics/departments/CustomerSupport/trend?from=2026-07-05&to=2026-07-08")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["department"], "CustomerSupport")
        items = data["items"]
        self.assertEqual(len(items), 4)

        # Day 1: 2026-07-05 (Sunday)
        # SUP001 joins on 2026-07-06 -> headcount=0 on 2026-07-05, weekend
        d0 = items[0]
        self.assertEqual(d0["date"], "2026-07-05")
        self.assertFalse(d0["is_working_day"])
        self.assertEqual(d0["headcount"], 0)
        self.assertEqual(d0["present_count"], 0.0)
        self.assertEqual(d0["late_count"], 0)
        self.assertIsNone(d0["attendance_rate"])
        self.assertIsNone(d0["moving_avg_7d"])

        # Day 2: 2026-07-06 (Monday)
        # SUP001 joined -> headcount=1, working day, 0 logs -> rate=0.0, moving_avg=0.0
        d1 = items[1]
        self.assertEqual(d1["date"], "2026-07-06")
        self.assertTrue(d1["is_working_day"])
        self.assertEqual(d1["headcount"], 1)
        self.assertEqual(d1["present_count"], 0.0)
        self.assertEqual(d1["late_count"], 0)
        self.assertEqual(d1["attendance_rate"], 0.0)
        self.assertEqual(d1["moving_avg_7d"], 0.0)

        # Day 3: 2026-07-07 (Tuesday)
        d2 = items[2]
        self.assertEqual(d2["date"], "2026-07-07")
        self.assertTrue(d2["is_working_day"])
        self.assertEqual(d2["headcount"], 1)
        self.assertEqual(d2["present_count"], 0.0)
        self.assertEqual(d2["late_count"], 0)
        self.assertEqual(d2["attendance_rate"], 0.0)
        self.assertEqual(d2["moving_avg_7d"], 0.0)

        # Day 4: 2026-07-08 (Wednesday)
        d3 = items[3]
        self.assertEqual(d3["date"], "2026-07-08")
        self.assertTrue(d3["is_working_day"])
        self.assertEqual(d3["headcount"], 1)
        self.assertEqual(d3["present_count"], 0.0)
        self.assertEqual(d3["late_count"], 0)
        self.assertEqual(d3["attendance_rate"], 0.0)
        self.assertEqual(d3["moving_avg_7d"], 0.0)


if __name__ == "__main__":
    unittest.main()
