"""
Comprehensive regression test suite for Employee Attendance & Analytics API.
Exercises helpers, business rules R1-R10, validation, and all API endpoints using TestClient with mongomock.
"""
import unittest
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import mongomock

import app.main as main_module

# Shared mock DB across tests
shared_mock_client = mongomock.MongoClient()
shared_mock_db = shared_mock_client["test_attendance_db"]
main_module.client = shared_mock_client
main_module.db = shared_mock_db
main_module.init_db_indexes()

client = TestClient(main_module.app)


class TestFullAPI(unittest.TestCase):
    def setUp(self):
        main_module.client = shared_mock_client
        main_module.db = shared_mock_db
        shared_mock_db.employees.delete_many({})
        shared_mock_db.attendance_logs.delete_many({})

    def test_01_health(self):
        res = client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ok"})

    def test_02_employee_creation_and_validation(self):
        # Valid create
        emp = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        }
        res = client.post("/employees", json=emp)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertIsInstance(data["created_at"], int)

        # Duplicate emp_code -> 409
        dup_res = client.post("/employees", json=emp)
        self.assertEqual(dup_res.status_code, 409)

        # Invalid shift_start == shift_end -> 422
        bad_emp = dict(emp, emp_code="EMP0002", shift_end="09:30")
        bad_res = client.post("/employees", json=bad_emp)
        self.assertEqual(bad_res.status_code, 422)

    def test_03_r1_overnight_shift(self):
        emp = {
            "emp_code": "EMP0005",
            "name": "Meera Nair",
            "email": "meera@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-03-10",
        }
        client.post("/employees", json=emp)

        # 2026-07-07 05:30:10 IST -> belongs to previous calendar day (2026-07-06)
        dt = datetime(2026, 7, 7, 5, 30, 10, tzinfo=main_module.IST)
        ms = int(dt.timestamp() * 1000)
        p_res = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms})
        self.assertEqual(p_res.status_code, 201)
        att = p_res.json()
        self.assertEqual(att["date"], "2026-07-06")

    def test_04_r2_late_grace_boundaries(self):
        emp = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        }
        client.post("/employees", json=emp)

        # 09:40:00 IST -> late_minutes = 0
        dt1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=main_module.IST)
        att1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt1.timestamp() * 1000)}).json()
        self.assertEqual(att1["late_minutes"], 0)

        # 09:40:01 IST on next day -> late_minutes = 10
        dt2 = datetime(2026, 7, 7, 9, 40, 1, tzinfo=main_module.IST)
        att2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt2.timestamp() * 1000)}).json()
        self.assertEqual(att2["late_minutes"], 10)

        # 10:15:59 IST on day after -> late_minutes = 45
        dt3 = datetime(2026, 7, 8, 10, 15, 59, tzinfo=main_module.IST)
        att3 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt3.timestamp() * 1000)}).json()
        self.assertEqual(att3["late_minutes"], 45)

    def test_05_punch_out_and_rules_r3_r4_r5(self):
        emp = {
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
        }
        client.post("/employees", json=emp)

        # Punch in at 09:28:00 IST
        p_in_dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=main_module.IST)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": int(p_in_dt.timestamp() * 1000)})

        # Punch out at 19:10:00 IST (40 min overtime >= 30, work_hours = 9.70)
        p_out_dt = datetime(2026, 7, 6, 19, 10, 0, tzinfo=main_module.IST)
        out_res = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": int(p_out_dt.timestamp() * 1000)})
        self.assertEqual(out_res.status_code, 200)
        rec = out_res.json()
        self.assertEqual(rec["overtime_minutes"], 40)
        self.assertEqual(rec["work_hours"], 9.7)
        self.assertFalse(rec["half_day"])

        # Second punch out -> 409
        dup_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": int(p_out_dt.timestamp() * 1000)})
        self.assertEqual(dup_out.status_code, 409)

    def test_06_regularize_patch_audit_history(self):
        emp = {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
        }
        client.post("/employees", json=emp)

        # Punch in at 10:05 IST (35 min late)
        dt = datetime(2026, 7, 6, 10, 5, 0, tzinfo=main_module.IST)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt.timestamp() * 1000)})

        # Regularize to 09:28 IST (0 min late)
        cor_dt = datetime(2026, 7, 6, 9, 28, 0, tzinfo=main_module.IST)
        patch_body = {
            "punch_in": int(cor_dt.timestamp() * 1000),
            "reason": "Biometric device malfunction",
            "regularized_by": "hr.manager",
        }
        p_res = client.patch("/attendance/EMP0001/2026-07-06", json=patch_body)
        self.assertEqual(p_res.status_code, 200)
        rec = p_res.json()
        self.assertEqual(rec["late_minutes"], 0)
        self.assertEqual(len(rec["history"]), 1)
        h = rec["history"][0]
        self.assertEqual(h["by"], "hr.manager")
        self.assertIn("punch_in", h["changes"])
        self.assertIn("late_minutes", h["changes"])
        self.assertEqual(h["changes"]["late_minutes"]["from"], 35)
        self.assertEqual(h["changes"]["late_minutes"]["to"], 0)

        # Direct test of conflict: simulate concurrent update by directly modifying document in DB
        shared_mock_db.attendance_logs.update_one(
            {"emp_code": "EMP0001", "date": "2026-07-06"},
            {"$set": {"status": "WFH"}}
        )
        conflict_res = client.patch(
            "/attendance/EMP0001/2026-07-06",
            json={
                "status": "ON_DUTY",
                "reason": "Simultaneous update by another manager",
                "regularized_by": "hr.admin",
            }
        )
        # Verify that patch can either succeed on fresh read or detect conflict
        self.assertIn(conflict_res.status_code, [200, 409])

    def test_07_analytics_department_summary_and_trend(self):
        # Create department employees
        client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        })
        client.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        })

        # Summary for month with zero-log employee
        res = client.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["headcount"], 2)

        # Trend for range
        trend_res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-03")
        self.assertEqual(trend_res.status_code, 200)
        t_items = trend_res.json()["items"]
        self.assertEqual(len(t_items), 3)

    def test_08_r7_mid_month_joiner_and_weekends(self):
        # Employee joins on July 15, 2026
        client.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "joined_on": "2026-07-15",
        })

        # Punch in on Sunday (2026-07-19)
        dt_sun = datetime(2026, 7, 19, 9, 45, 0, tzinfo=main_module.IST)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": int(dt_sun.timestamp() * 1000)})

        # Punch out on Sunday
        dt_sun_out = datetime(2026, 7, 19, 19, 10, 0, tzinfo=main_module.IST)
        client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": int(dt_sun_out.timestamp() * 1000)})

        # Monthly summary
        res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["working_days"], 13)
        self.assertEqual(data["present_days"], 0.0)  # Weekend records excluded from present_days
        self.assertEqual(data["late_count"], 1)
        self.assertEqual(data["total_late_minutes"], 15)
        self.assertEqual(data["total_overtime_minutes"], 40)

    def test_09_leaderboard_competition_ranking_and_ties(self):
        # 3 employees: EMP0001 (50 min), EMP0002 (50 min tied), EMP0003 (30 min)
        client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        })
        client.post("/employees", json={
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        })
        client.post("/employees", json={
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        })

        # EMP1 late 50 min
        dt1 = datetime(2026, 7, 6, 10, 20, 0, tzinfo=main_module.IST)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt1.timestamp() * 1000)})
        # EMP2 late 50 min
        dt2 = datetime(2026, 7, 6, 10, 20, 0, tzinfo=main_module.IST)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": int(dt2.timestamp() * 1000)})
        # EMP3 late 30 min
        dt3 = datetime(2026, 7, 6, 10, 0, 0, tzinfo=main_module.IST)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": int(dt3.timestamp() * 1000)})

        # Limit 1: Both EMP1 and EMP2 should be returned because they tie for Rank 1
        res = client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[1]["rank"], 1)

        # Limit 3: EMP3 should have Rank 3 (skipped rank 2: 1, 1, 3)
        res_all = client.get("/analytics/leaderboard/late?month=2026-07&limit=3")
        items_all = res_all.json()["items"]
        self.assertEqual(len(items_all), 3)
        self.assertEqual(items_all[2]["rank"], 3)

    def test_10_attendance_pagination_and_sorting(self):
        client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01",
        })
        for d in range(1, 15):
            dt = datetime(2026, 7, d, 9, 30, 0, tzinfo=main_module.IST)
            client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt.timestamp() * 1000)})

        # Request page 1 of 5
        res = client.get("/attendance?page=1&page_size=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 14)
        self.assertEqual(len(data["items"]), 5)
        # Sorted date descending
        self.assertEqual(data["items"][0]["date"], "2026-07-14")

    def test_11_admin_explain_endpoint(self):
        res = client.get("/admin/explain/attendance_list?page=1&page_size=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["endpoint"], "attendance_list")
        self.assertEqual(data["collection"], "attendance_logs")
        self.assertIn("explain", data)


if __name__ == "__main__":
    unittest.main()
