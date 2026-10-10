import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from pymongo import MongoClient
import app.main as app_main


class TestPhase2Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.test_db = cls.client["test_attendance_db_phase2_unit"]
        app_main.db = cls.test_db
        app_main.init_indexes(cls.test_db)
        cls.tc = TestClient(app_main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database("test_attendance_db_phase2_unit")
        cls.client.close()

    def setUp(self):
        self.test_db.employees.delete_many({})
        self.test_db.attendance_logs.delete_many({})

    def test_health(self):
        r = self.tc.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

        class FailingClient:
            class Admin:
                def command(self, cmd):
                    raise Exception("DB offline")
            admin = Admin()

        orig_client = app_main.client
        app_main.client = FailingClient()
        try:
            r_fail = self.tc.get("/health")
            self.assertEqual(r_fail.status_code, 503)
            self.assertEqual(r_fail.json(), {"detail": "Database unavailable"})
        finally:
            app_main.client = orig_client

    def test_employee_crud(self):
        emp1 = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05"
        }
        r = self.tc.post("/employees", json=emp1)
        self.assertEqual(r.status_code, 201)
        data1 = r.json()
        self.assertEqual(data1["emp_code"], "EMP0001")
        self.assertNotIn("_id", data1)
        self.assertIsInstance(data1["created_at"], int)

        # Duplicate emp_code -> 409
        r_dup = self.tc.post("/employees", json=emp1)
        self.assertEqual(r_dup.status_code, 409)

        # Bad emp_code -> 422
        r_bad_code = self.tc.post("/employees", json={**emp1, "emp_code": "BAD_CODE"})
        self.assertEqual(r_bad_code.status_code, 422)

        # Bad shift -> 422
        r_same_shift = self.tc.post("/employees", json={**emp1, "emp_code": "EMP0002", "shift_end": "09:30"})
        self.assertEqual(r_same_shift.status_code, 422)

        # List employees & filter
        emp2 = {
            "emp_code": "EMP0002",
            "name": "Meera Nair",
            "email": "meera@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-03-10"
        }
        self.tc.post("/employees", json=emp2)

        r_list = self.tc.get("/employees")
        self.assertEqual(r_list.status_code, 200)
        self.assertEqual(r_list.json()["total"], 2)

        r_filt = self.tc.get("/employees?department=Engineering")
        self.assertEqual(r_filt.status_code, 200)
        self.assertEqual(r_filt.json()["total"], 1)
        self.assertEqual(r_filt.json()["items"][0]["emp_code"], "EMP0001")

    def test_punch_in_and_attendance_list(self):
        emp1 = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05"
        }
        self.tc.post("/employees", json=emp1)

        # 09:28 IST -> on time, late_minutes=0
        dt_on_time = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        ms_on_time = int(dt_on_time.timestamp()) * 1000
        r_punch = self.tc.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_on_time})
        self.assertEqual(r_punch.status_code, 201)
        data_p = r_punch.json()
        self.assertEqual(data_p["date"], "2026-07-06")
        self.assertEqual(data_p["late_minutes"], 0)
        self.assertEqual(data_p["status"], "PRESENT")

        # Duplicate punch in same day -> 409
        r_punch_dup = self.tc.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_on_time})
        self.assertEqual(r_punch_dup.status_code, 409)

        # Attendance list
        r_att = self.tc.get("/attendance?emp_code=EMP0001")
        self.assertEqual(r_att.status_code, 200)
        self.assertEqual(r_att.json()["total"], 1)

    def test_concurrency(self):
        emp_race = {
            "emp_code": "EMP9999",
            "name": "Racer",
            "email": "racer@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(lambda: self.tc.post("/employees", json=emp_race))
            f2 = executor.submit(lambda: self.tc.post("/employees", json=emp_race))
            codes = sorted([f1.result().status_code, f2.result().status_code])
        self.assertEqual(codes, [201, 409])


if __name__ == "__main__":
    unittest.main()
