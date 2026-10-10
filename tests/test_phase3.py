import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from pymongo import MongoClient
import app.main as app_main


class TestPhase3Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cls.test_db = cls.client["test_attendance_db_phase3_unit"]
        app_main.db = cls.test_db
        app_main.init_indexes(cls.test_db)
        cls.tc = TestClient(app_main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database("test_attendance_db_phase3_unit")
        cls.client.close()

    def setUp(self):
        self.test_db.employees.delete_many({})
        self.test_db.attendance_logs.delete_many({})

    def test_punch_out_and_boundaries(self):
        self.tc.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05"
        })

        # Punch in: 09:28 IST (03:58 UTC)
        p_in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        self.tc.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": int(p_in_dt.timestamp()) * 1000
        })

        # Punch out: 18:35 IST (13:05 UTC)
        # 9 hours 7 minutes = 32820 seconds / 3600 = 9.11666... -> 9.12 hours
        # Shift end is 18:30, out at 18:35 -> overtime 5 min < 30 -> 0 overtime
        # work_hours >= 4.50 -> half_day = False
        p_out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        r_out = self.tc.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": int(p_out_dt.timestamp()) * 1000
        })
        self.assertEqual(r_out.status_code, 200)
        d_out = r_out.json()
        self.assertEqual(d_out["work_hours"], 9.12)
        self.assertEqual(d_out["overtime_minutes"], 0)
        self.assertFalse(d_out["half_day"])

        # Second punch out -> 409 conflict
        r_out_again = self.tc.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": int(p_out_dt.timestamp()) * 1000
        })
        self.assertEqual(r_out_again.status_code, 409)

    def test_regularization(self):
        self.tc.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05"
        })

        # Seed record with late=35
        p_in_dt = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
        p_out_dt = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
        self.test_db.attendance_logs.insert_one({
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": p_in_dt,
            "punch_out": p_out_dt,
            "work_hours": 8.42,
            "late_minutes": 35,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })

        # Correct punch_in to 09:28 IST (03:58 UTC)
        new_pin_dt = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
        r_patch = self.tc.patch("/attendance/EMP0001/2026-07-07", json={
            "punch_in": int(new_pin_dt.timestamp()) * 1000,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin"
        })
        self.assertEqual(r_patch.status_code, 200)
        d_patch = r_patch.json()
        self.assertEqual(d_patch["late_minutes"], 0)
        self.assertEqual(d_patch["work_hours"], 9.03)
        self.assertEqual(len(d_patch["history"]), 1)
        h = d_patch["history"][0]
        self.assertEqual(h["by"], "hr.admin")
        self.assertEqual(h["changes"]["late_minutes"], {"from": 35, "to": 0})


if __name__ == "__main__":
    unittest.main()
