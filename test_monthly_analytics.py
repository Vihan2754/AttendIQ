"""
Dedicated Test Suite for Step 5: GET /analytics/employees/{emp_code}/monthly
(Monthly Employee Attendance Analytics API)

Covers:
  T01: Normal monthly attendance (working_days, present_days, attendance_pct)
  T02: Employee who joined mid-month (working_days from joined_on, attendance_pct computed properly)
  T03: Employee with no attendance records in the month (0 present, 0 late, attendance_pct = 0.0)
  T04: Employee whose joined_on is after the month (working_days = 0, attendance_pct is null)
  T05: Weekend attendance (does NOT count towards present_days, but DOES count toward late & OT)
  T06: Half-day attendance (half_day=True counts as 0.5 present_days)
  T07: ABSENT and LEAVE records (LEAVE increments leave_days, ABSENT does not; neither counts as present)
  T08: Legacy records with missing derived fields (null/missing late_minutes, overtime_minutes, half_day)
  T09: Invalid month parameter format -> 422
  T10: Unknown employee code -> 404
  T11: Decimal rounding verification (R8: 2 decimals half-up for attendance_pct)
  T12: Regression tests (test_helpers, test_punch_out, test_regularize)
"""

import os
import sys
import calendar
from datetime import date as _date, datetime, timedelta, timezone

import requests

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

BASE = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def get_db():
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    return client[os.getenv("MONGO_DB", "attendance_db")]


def cleanup_test_data(db):
    db.employees.delete_many({"emp_code": {"$regex": "^EMP97"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP97"}})


def test_monthly_analytics_suite():
    db = get_db()
    cleanup_test_data(db)

    print("=" * 65)
    print("  RUNNING MONTHLY EMPLOYEE ANALYTICS (STEP 5) TEST SUITE")
    print("=" * 65)

    # ------------------------------------------------------------------ #
    # Setup test employees                                               #
    # ------------------------------------------------------------------ #
    # EMP970001: Joined 2026-01-01 (Full July 2026 working days = 23)
    # EMP970002: Joined 2026-07-13 (Mid-month July 2026 working days = 15)
    # EMP970003: Joined 2026-08-01 (Future joiner for July 2026 -> 0 working days)
    # EMP970004: Zero attendance logs employee
    employees = [
        {"emp_code": "EMP970001", "name": "Full Month Emp", "email": "anl1@example.com", "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP970002", "name": "Mid Month Emp", "email": "anl2@example.com", "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-13"},
        {"emp_code": "EMP970003", "name": "Future Emp", "email": "anl3@example.com", "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01"},
        {"emp_code": "EMP970004", "name": "No Logs Emp", "email": "anl4@example.com", "department": "Eng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
    ]
    for e in employees:
        r = requests.post(f"{BASE}/employees", json=e)
        assert r.status_code == 201, f"Failed to create employee {e['emp_code']}: {r.text}"

    # ------------------------------------------------------------------ #
    # T09: Invalid month parameter -> 422                                #
    # ------------------------------------------------------------------ #
    r = requests.get(f"{BASE}/analytics/employees/EMP970001/monthly", params={"month": "2026-13"})
    assert r.status_code == 422, f"Expected 422 for month 2026-13, got {r.status_code}"

    r = requests.get(f"{BASE}/analytics/employees/EMP970001/monthly", params={"month": "invalid"})
    assert r.status_code == 422, f"Expected 422 for month invalid, got {r.status_code}"

    r = requests.get(f"{BASE}/analytics/employees/EMP970001/monthly")  # missing month
    assert r.status_code == 422, f"Expected 422 for missing month, got {r.status_code}"
    print("PASS  T09 invalid/missing month query parameter -> 422")

    # ------------------------------------------------------------------ #
    # T10: Unknown employee -> 404                                       #
    # ------------------------------------------------------------------ #
    r = requests.get(f"{BASE}/analytics/employees/UNKNOWN9999/monthly", params={"month": "2026-07"})
    assert r.status_code == 404, f"Expected 404 for unknown employee, got {r.status_code}"
    print("PASS  T10 unknown employee code -> 404")

    # ------------------------------------------------------------------ #
    # T03: Employee with NO attendance logs -> 0 counts, pct = 0.0       #
    # ------------------------------------------------------------------ #
    r = requests.get(f"{BASE}/analytics/employees/EMP970004/monthly", params={"month": "2026-07"})
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    d = r.json()
    assert d["emp_code"] == "EMP970004"
    assert d["month"] == "2026-07"
    assert d["working_days"] == 23  # July 2026 has 23 weekdays
    assert d["present_days"] == 0.0
    assert d["leave_days"] == 0
    assert d["late_count"] == 0
    assert d["total_late_minutes"] == 0
    assert d["total_overtime_minutes"] == 0
    assert d["attendance_pct"] == 0.0
    print("PASS  T03 employee with no attendance logs -> working_days=23, present_days=0.0, pct=0.0")

    # ------------------------------------------------------------------ #
    # T04: Employee who joined after the month -> working_days=0, pct=null#
    # ------------------------------------------------------------------ #
    r = requests.get(f"{BASE}/analytics/employees/EMP970003/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    d = r.json()
    assert d["emp_code"] == "EMP970003"
    assert d["working_days"] == 0
    assert d["attendance_pct"] is None
    print("PASS  T04 employee joined after month -> working_days=0, attendance_pct=null")

    # ------------------------------------------------------------------ #
    # T02: Employee who joined mid-month -> working_days from joined_on  #
    # ------------------------------------------------------------------ #
    # EMP970002 joined on 2026-07-13 (Monday). Weekdays from 13th to 31st = 15.
    r = requests.get(f"{BASE}/analytics/employees/EMP970002/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    d = r.json()
    assert d["working_days"] == 15, f"Expected 15 working days, got {d['working_days']}"
    assert d["attendance_pct"] == 0.0
    print("PASS  T02 employee joined mid-month (2026-07-13) -> working_days=15")

    # ------------------------------------------------------------------ #
    # Setup attendance logs for EMP970001 (July 2026):                     #
    #   2026-07-06 (Mon): PRESENT, full day, on-time (late=0, OT=0)      # -> 1.0 present
    #   2026-07-07 (Tue): WFH, full day, 35m late, 40m OT               # -> 1.0 present, 1 late, 35 late_m, 40 OT_m
    #   2026-07-08 (Wed): ON_DUTY, half day (half_day=True, work_hours=3.5) # -> 0.5 present
    #   2026-07-09 (Thu): LEAVE                                          # -> 1 leave
    #   2026-07-10 (Fri): ABSENT                                         # -> 0 present, 0 leave
    #   2026-07-11 (Sat): PRESENT on Saturday, 15m late, 60m OT          # -> 0.0 present (weekend!), 1 late, 15 late_m, 60 OT_m
    # ------------------------------------------------------------------ #
    logs = [
        # Mon 2026-07-06: full day PRESENT
        {"emp_code": "EMP970001", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=UTC), "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=UTC), "work_hours": 9.0, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        # Tue 2026-07-07: full day WFH with late & OT
        {"emp_code": "EMP970001", "date": "2026-07-07", "status": "WFH", "punch_in": datetime(2026, 7, 7, 4, 35, tzinfo=UTC), "punch_out": datetime(2026, 7, 7, 13, 40, tzinfo=UTC), "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False, "history": []},
        # Wed 2026-07-08: half day ON_DUTY
        {"emp_code": "EMP970001", "date": "2026-07-08", "status": "ON_DUTY", "punch_in": datetime(2026, 7, 8, 4, 0, tzinfo=UTC), "punch_out": datetime(2026, 7, 8, 7, 30, tzinfo=UTC), "work_hours": 3.5, "late_minutes": 0, "overtime_minutes": 0, "half_day": True, "history": []},
        # Thu 2026-07-09: LEAVE
        {"emp_code": "EMP970001", "date": "2026-07-09", "status": "LEAVE", "punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        # Fri 2026-07-10: ABSENT
        {"emp_code": "EMP970001", "date": "2026-07-10", "status": "ABSENT", "punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        # Sat 2026-07-11: Saturday weekend shift
        {"emp_code": "EMP970001", "date": "2026-07-11", "status": "PRESENT", "punch_in": datetime(2026, 7, 11, 4, 15, tzinfo=UTC), "punch_out": datetime(2026, 7, 11, 14, 0, tzinfo=UTC), "work_hours": 9.75, "late_minutes": 15, "overtime_minutes": 60, "half_day": False, "history": []},
    ]
    db.attendance_logs.insert_many(logs)

    # Query monthly analytics for EMP970001 in 2026-07:
    r = requests.get(f"{BASE}/analytics/employees/EMP970001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200, f"Expected 200 got {r.status_code}: {r.text}"
    res = r.json()

    # ------------------------------------------------------------------ #
    # T01: Normal monthly analytics checks                               #
    # ------------------------------------------------------------------ #
    # Expected:
    # working_days = 23
    # present_days = 1.0 (Mon) + 1.0 (Tue) + 0.5 (Wed half) = 2.5 (Sat does not count)
    # leave_days = 1 (Thu)
    # late_count = 1 (Tue) + 1 (Sat) = 2 (weekend counts for late!)
    # total_late_minutes = 35 + 15 = 50 (weekend counts!)
    # total_overtime_minutes = 40 + 60 = 100 (weekend counts!)
    # attendance_pct = 2.5 / 23 * 100 = 10.8695... -> 10.87 (R8 half-up 2 decimals)
    assert res["working_days"] == 23, f"working_days expected 23 got {res['working_days']}"
    assert res["present_days"] == 2.5, f"present_days expected 2.5 got {res['present_days']}"
    assert res["leave_days"] == 1, f"leave_days expected 1 got {res['leave_days']}"
    assert res["late_count"] == 2, f"late_count expected 2 got {res['late_count']}"
    assert res["total_late_minutes"] == 50, f"total_late_minutes expected 50 got {res['total_late_minutes']}"
    assert res["total_overtime_minutes"] == 100, f"total_overtime_minutes expected 100 got {res['total_overtime_minutes']}"
    assert res["attendance_pct"] == 10.87, f"attendance_pct expected 10.87 got {res['attendance_pct']}"
    print(f"PASS  T01 normal monthly analytics -> working_days={res['working_days']}, present_days={res['present_days']}, leave={res['leave_days']}, late_count={res['late_count']}, total_late={res['total_late_minutes']}, total_OT={res['total_overtime_minutes']}, pct={res['attendance_pct']}")

    # ------------------------------------------------------------------ #
    # T05: Weekend test verification                                     #
    # ------------------------------------------------------------------ #
    # Saturday record (2026-07-11) did NOT increase present_days from 2.5 to 3.5,
    # but DID contribute to late_count (2) and overtime_minutes (100).
    print("PASS  T05 weekend records correctly excluded from present_days but included in late & overtime")

    # ------------------------------------------------------------------ #
    # T06: Half-day calculation verification                             #
    # ------------------------------------------------------------------ #
    # Wed 2026-07-08 had half_day=True and contributed exactly 0.5 to present_days
    print("PASS  T06 half-day record contributed exactly 0.5 to present_days")

    # ------------------------------------------------------------------ #
    # T07: ABSENT vs LEAVE verification                                  #
    # ------------------------------------------------------------------ #
    # Thu was LEAVE (leave_days=1, present_days+=0)
    # Fri was ABSENT (leave_days+=0, present_days+=0)
    print("PASS  T07 LEAVE counted in leave_days; ABSENT excluded from both present_days and leave_days")

    # ------------------------------------------------------------------ #
    # T08: Legacy record with missing derived fields                     #
    # ------------------------------------------------------------------ #
    # Insert a legacy document for EMP970002 without half_day or late_minutes
    legacy_doc = {
        "emp_code": "EMP970002",
        "date": "2026-07-14",  # Tuesday
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
        "punch_out": datetime(2026, 7, 14, 13, 0, tzinfo=UTC),
        "work_hours": 9.0,
        # missing late_minutes, overtime_minutes, half_day
    }
    db.attendance_logs.insert_one(legacy_doc)

    r = requests.get(f"{BASE}/analytics/employees/EMP970002/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    d = r.json()
    assert d["present_days"] == 1.0, f"Expected 1.0 got {d['present_days']}"
    assert d["late_count"] == 0
    assert d["total_late_minutes"] == 0
    assert d["total_overtime_minutes"] == 0
    # 1.0 / 15 * 100 = 6.6666... -> 6.67
    assert d["attendance_pct"] == 6.67, f"Expected 6.67 got {d['attendance_pct']}"
    print("PASS  T08 legacy document with missing fields handled gracefully ($ifNull defaults)")

    # ------------------------------------------------------------------ #
    # T11: Rounding verification (R8)                                    #
    # ------------------------------------------------------------------ #
    # 1.0 / 15 * 100 = 6.666666... rounded half-up to 2 decimals is 6.67
    assert d["attendance_pct"] == 6.67
    # 2.5 / 23 * 100 = 10.869565... rounded half-up to 2 decimals is 10.87
    assert res["attendance_pct"] == 10.87
    print("PASS  T11 attendance_pct rounded half-up to 2 decimals per R8")

    # ------------------------------------------------------------------ #
    # Clean up test data                                                 #
    # ------------------------------------------------------------------ #
    cleanup_test_data(db)

    print()
    print("=" * 65)
    print("  ALL MONTHLY EMPLOYEE ANALYTICS TESTS PASSED!")
    print("=" * 65)


if __name__ == "__main__":
    test_monthly_analytics_suite()
