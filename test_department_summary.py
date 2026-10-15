"""
Test Suite for Step 6: GET /analytics/departments/summary
(Department Summary Analytics API)

Covers:
  T01: Normal department statistics (headcount, present_days, avg_work_hours, late_count, total_late_minutes, leave_count, on_duty_count)
  T02: Employees with no attendance records (must be included in headcount)
  T03: Employees joining during the reporting period (included in headcount)
  T04: Employees joining after the reporting period (excluded from headcount per R9)
  T05: Weekend attendance (excluded from present_days per R7, included in late totals and avg_work_hours)
  T06: Half-day attendance (half_day=True counts as 0.5 present_days)
  T07: ABSENT and LEAVE records (LEAVE counted in leave_count, ABSENT not present, neither in present_days)
  T08: Legacy records with missing derived fields (null/missing late_minutes, half_day)
  T09: Empty departments (headcount 0 omitted) and departments with 0 logs (avg_work_hours is null)
  T10: Exact rounding behavior (R8: 2 decimals half-up for avg_work_hours)
  T11: Response schema validation, department filter, and 422 error handling
"""

import os
import sys
import calendar
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import requests

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

BASE = "http://localhost:8000"


def get_db():
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    return client[os.getenv("MONGO_DB", "attendance_db")]


def cleanup_test_data(db):
    db.employees.delete_many({"emp_code": {"$regex": "^EMP96"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP96"}})


def test_department_summary_suite():
    db = get_db()
    cleanup_test_data(db)

    print("=" * 70)
    print("  RUNNING DEPARTMENT SUMMARY ANALYTICS (STEP 6) TEST SUITE")
    print("=" * 70)

    # ------------------------------------------------------------------ #
    # T11: Error handling & validation                                   #
    # ------------------------------------------------------------------ #
    print("\n[T11] Error handling & month validation...")
    r = requests.get(f"{BASE}/analytics/departments/summary")
    assert r.status_code == 422, f"Expected 422 for missing month, got {r.status_code}"

    r = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-13"})
    assert r.status_code == 422, f"Expected 422 for month 2026-13, got {r.status_code}"

    r = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026/07"})
    assert r.status_code == 422, f"Expected 422 for month 2026/07, got {r.status_code}"

    r = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "invalid"})
    assert r.status_code == 422, f"Expected 422 for invalid month, got {r.status_code}"
    print("  PASS: 422 returned for missing and invalid month formats.")

    # ------------------------------------------------------------------ #
    # Setup test employees                                               #
    # ------------------------------------------------------------------ #
    # DeptTestA:
    #   EMP960001: Joined 2026-01-01
    #   EMP960002: Joined 2026-01-01
    #   EMP960003: Joined 2026-01-01 (Zero attendance logs)
    #   EMP960004: Joined 2026-07-15 (Mid-period joiner)
    #   EMP960005: Joined 2026-08-01 (Future joiner for July 2026)
    #
    # DeptTestB:
    #   EMP960006: Joined 2026-01-01 (Weekend attendance)
    #   EMP960007: Joined 2026-01-01 (Half-day attendance)
    #   EMP960008: Joined 2026-01-01 (ABSENT, LEAVE, ON_DUTY)
    #   EMP960009: Joined 2026-01-01 (Legacy record with missing fields)
    #
    # DeptTestZero:
    #   EMP960010: Joined 2026-01-01 (Zero logs)
    #
    # DeptFutureOnly:
    #   EMP960011: Joined 2026-08-15 (Headcount 0 in July 2026 -> omitted)
    employees = [
        {"emp_code": "EMP960001", "name": "Emp A1", "email": "a1@test.com", "department": "DeptTestA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960002", "name": "Emp A2", "email": "a2@test.com", "department": "DeptTestA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960003", "name": "Emp A3 (No Logs)", "email": "a3@test.com", "department": "DeptTestA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960004", "name": "Emp A4 (Mid Joiner)", "email": "a4@test.com", "department": "DeptTestA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-15"},
        {"emp_code": "EMP960005", "name": "Emp A5 (Future Joiner)", "email": "a5@test.com", "department": "DeptTestA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01"},
        {"emp_code": "EMP960006", "name": "Emp B1 (Weekend)", "email": "b1@test.com", "department": "DeptTestB", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960007", "name": "Emp B2 (Half Day)", "email": "b2@test.com", "department": "DeptTestB", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960008", "name": "Emp B3 (Mixed Status)", "email": "b3@test.com", "department": "DeptTestB", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960009", "name": "Emp B4 (Legacy)", "email": "b4@test.com", "department": "DeptTestB", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960010", "name": "Emp Zero", "email": "zero@test.com", "department": "DeptTestZero", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP960011", "name": "Emp Future", "email": "future@test.com", "department": "DeptFutureOnly", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-15"},
    ]
    for emp in employees:
        r = requests.post(f"{BASE}/employees", json=emp)
        assert r.status_code == 201, f"Failed to create {emp['emp_code']}: {r.text}"

    # Insert test attendance logs
    logs = [
        # DeptTestA:
        # EMP960001: 2 days present
        {
            "emp_code": "EMP960001",
            "date": "2026-07-06", # Mon
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
            "work_hours": 8.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        {
            "emp_code": "EMP960001",
            "date": "2026-07-07", # Tue
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 7, 4, 15, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 7, 13, 15, tzinfo=timezone.utc),
            "work_hours": 9.0,
            "late_minutes": 15,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # EMP960002: 1 day WFH
        {
            "emp_code": "EMP960002",
            "date": "2026-07-08", # Wed
            "status": "WFH",
            "punch_in": datetime(2026, 7, 8, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 8, 11, 30, tzinfo=timezone.utc),
            "work_hours": 7.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # EMP960003: No logs at all
        # EMP960004: Mid-joiner (joined 2026-07-15), 1 log on 2026-07-16
        {
            "emp_code": "EMP960004",
            "date": "2026-07-16", # Thu
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
            "work_hours": 8.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # EMP960005: Future joiner (joined 2026-08-01) - no logs in July

        # DeptTestB:
        # EMP960006: Weekend record (2026-07-11 is Saturday)
        {
            "emp_code": "EMP960006",
            "date": "2026-07-11", # Sat
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 11, 4, 20, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 11, 10, 20, tzinfo=timezone.utc),
            "work_hours": 6.0,
            "late_minutes": 20,
            "overtime_minutes": 30,
            "half_day": False,
            "history": [],
        },
        # EMP960007: Half-day record (<4.5 hours)
        {
            "emp_code": "EMP960007",
            "date": "2026-07-13", # Mon
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 13, 7, 30, tzinfo=timezone.utc),
            "work_hours": 3.5,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": True,
            "history": [],
        },
        # EMP960008: LEAVE, ABSENT, ON_DUTY
        {
            "emp_code": "EMP960008",
            "date": "2026-07-14", # Tue
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
            "emp_code": "EMP960008",
            "date": "2026-07-15", # Wed
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
            "emp_code": "EMP960008",
            "date": "2026-07-16", # Thu
            "status": "ON_DUTY",
            "punch_in": datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
            "work_hours": 8.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
        # EMP960009: Legacy record missing half_day and late_minutes
        {
            "emp_code": "EMP960009",
            "date": "2026-07-17", # Fri
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc),
            "punch_out": datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc),
            "work_hours": 8.0,
            # no half_day, no late_minutes, no overtime_minutes
        },
    ]
    db.attendance_logs.insert_many(logs)

    # ------------------------------------------------------------------ #
    # T01, T02, T03, T04: DeptTestA verification                        #
    # ------------------------------------------------------------------ #
    print("\n[T01-T04] DeptTestA stats, 0-log emp, mid-joiner, future joiner exclusion...")
    r = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-07", "department": "DeptTestA"})
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["month"] == "2026-07"
    assert len(body["items"]) == 1, f"Expected 1 item for DeptTestA, got {len(body['items'])}"
    item_a = body["items"][0]

    # Headcount: EMP960001, EMP960002, EMP960003 (0 logs), EMP960004 (mid joiner) = 4.
    # EMP960005 joined 2026-08-01, so EXCLUDED from July (R9).
    assert item_a["department"] == "DeptTestA"
    assert item_a["headcount"] == 4, f"Expected headcount 4, got {item_a['headcount']}"

    # Present days:
    # EMP960001: 2.0 (Jul 6, Jul 7)
    # EMP960002: 1.0 (Jul 8 WFH)
    # EMP960004: 1.0 (Jul 16)
    # Total = 4.0
    assert item_a["present_days"] == 4.0, f"Expected present_days 4.0, got {item_a['present_days']}"

    # avg_work_hours: (8.0 + 9.0 + 7.5 + 8.0) / 4 = 32.5 / 4 = 8.125 -> half-up rounds to 8.13
    assert item_a["avg_work_hours"] == 8.13, f"Expected avg_work_hours 8.13, got {item_a['avg_work_hours']}"

    # Late:
    # EMP960001 Jul 7 has late_minutes = 15.
    assert item_a["late_count"] == 1, f"Expected late_count 1, got {item_a['late_count']}"
    assert item_a["total_late_minutes"] == 15, f"Expected total_late_minutes 15, got {item_a['total_late_minutes']}"
    assert item_a["leave_count"] == 0, f"Expected leave_count 0, got {item_a['leave_count']}"
    assert item_a["on_duty_count"] == 0, f"Expected on_duty_count 0, got {item_a['on_duty_count']}"
    print("  PASS: DeptTestA metrics exact (Headcount 4, Present 4.0, Avg Hours 8.13, Late 1/15).")

    # Verify future month for EMP960005: in 2026-08, EMP960005 IS included!
    r_aug = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-08", "department": "DeptTestA"})
    assert r_aug.status_code == 200
    item_a_aug = r_aug.json()["items"][0]
    assert item_a_aug["headcount"] == 5, f"Expected headcount 5 in August, got {item_a_aug['headcount']}"
    print("  PASS: EMP960005 included in August headcount (5) after joining 2026-08-01.")

    # ------------------------------------------------------------------ #
    # T05, T06, T07, T08: DeptTestB verification                        #
    # ------------------------------------------------------------------ #
    print("\n[T05-T08] DeptTestB: Weekend, Half-Day, ABSENT/LEAVE/ON_DUTY, Legacy records...")
    r = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-07", "department": "DeptTestB"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    item_b = body["items"][0]

    # Headcount: 4 employees (EMP960006, 07, 08, 09)
    assert item_b["headcount"] == 4, f"Expected headcount 4, got {item_b['headcount']}"

    # Present days (Mon-Fri only):
    # EMP960006: Jul 11 is Saturday -> 0.0 (per R7)
    # EMP960007: Jul 13 is Monday half-day -> 0.5 (per R5)
    # EMP960008: Jul 14 LEAVE (0), Jul 15 ABSENT (0), Jul 16 ON_DUTY (1.0)
    # EMP960009: Jul 17 Friday legacy record (missing half_day -> defaults False) -> 1.0
    # Total present_days = 0.0 + 0.5 + 1.0 + 1.0 = 2.5
    assert item_b["present_days"] == 2.5, f"Expected present_days 2.5, got {item_b['present_days']}"

    # avg_work_hours:
    # Records with present-status (PRESENT, WFH, ON_DUTY) and non-null work_hours:
    # EMP960006: 6.0 (Saturday is PRESENT, work_hours=6.0)
    # EMP960007: 3.5 (Monday, work_hours=3.5)
    # EMP960008: 8.0 (Thursday ON_DUTY, work_hours=8.0)
    # EMP960009: 8.0 (Friday, work_hours=8.0)
    # Mean = (6.0 + 3.5 + 8.0 + 8.0) / 4 = 25.5 / 4 = 6.375 -> half-up rounds to 6.38
    assert item_b["avg_work_hours"] == 6.38, f"Expected avg_work_hours 6.38, got {item_b['avg_work_hours']}"

    # Late totals (per R7 weekend records contribute):
    # EMP960006 Saturday late_minutes = 20 -> late_count = 1, total_late_minutes = 20
    assert item_b["late_count"] == 1, f"Expected late_count 1, got {item_b['late_count']}"
    assert item_b["total_late_minutes"] == 20, f"Expected total_late_minutes 20, got {item_b['total_late_minutes']}"

    # Leave & On Duty counts:
    # EMP960008 has 1 LEAVE and 1 ON_DUTY. ABSENT does not increment leave_count.
    assert item_b["leave_count"] == 1, f"Expected leave_count 1, got {item_b['leave_count']}"
    assert item_b["on_duty_count"] == 1, f"Expected on_duty_count 1, got {item_b['on_duty_count']}"
    print("  PASS: DeptTestB exact (Present 2.5, Avg Hours 6.38, Weekend Late 1/20, Leave 1, On-Duty 1).")

    # ------------------------------------------------------------------ #
    # T09: Empty departments and zero logs department                    #
    # ------------------------------------------------------------------ #
    print("\n[T09] Empty departments and zero logs department...")
    # DeptTestZero has EMP960010 (joined 2026-01-01) with NO logs
    r = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-07", "department": "DeptTestZero"})
    assert r.status_code == 200
    item_zero = r.json()["items"][0]
    assert item_zero["headcount"] == 1
    assert item_zero["present_days"] == 0.0
    assert item_zero["avg_work_hours"] is None, f"Expected null avg_work_hours, got {item_zero['avg_work_hours']}"
    assert item_zero["late_count"] == 0
    assert item_zero["total_late_minutes"] == 0
    assert item_zero["leave_count"] == 0
    assert item_zero["on_duty_count"] == 0
    print("  PASS: Department with zero logs has headcount 1 and null avg_work_hours.")

    # DeptFutureOnly has EMP960011 (joined 2026-08-15) -> 0 headcount in July 2026 -> omitted!
    r = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-07", "department": "DeptFutureOnly"})
    assert r.status_code == 200
    assert r.json()["items"] == [], f"Expected empty items for 0-headcount department, got {r.json()['items']}"
    print("  PASS: 0-headcount department is omitted from response items.")

    # Query without department filter: verify ascending sort by department name
    r_all = requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-07"})
    assert r_all.status_code == 200
    depts = [it["department"] for it in r_all.json()["items"]]
    assert depts == sorted(depts), f"Items not sorted by department ascending: {depts}"
    assert "DeptTestA" in depts and "DeptTestB" in depts and "DeptTestZero" in depts
    assert "DeptFutureOnly" not in depts
    print("  PASS: All departments returned sorted ascending; DeptFutureOnly omitted.")

    # ------------------------------------------------------------------ #
    # T10: Exact rounding verification (R8: half-up)                     #
    # ------------------------------------------------------------------ #
    print("\n[T10] Rounding verification...")
    # 8.125 -> 8.13 verified in DeptTestA
    # 6.375 -> 6.38 verified in DeptTestB
    assert item_a["avg_work_hours"] == 8.13
    assert item_b["avg_work_hours"] == 6.38
    print("  PASS: R8 half-up rounding verified on real calculations (8.125->8.13, 6.375->6.38).")

    # Cleanup
    cleanup_test_data(db)
    print("\n" + "=" * 70)
    print("  ALL 11 DEPARTMENT SUMMARY TESTS PASSED!")
    print("=" * 70)


if __name__ == "__main__":
    test_department_summary_suite()
