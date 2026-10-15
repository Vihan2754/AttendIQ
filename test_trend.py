"""
Test Suite for Step 8: GET /analytics/departments/{department}/trend
(Department Daily Attendance Trend API)

Covers:
  T01: Parameter validation & HTTP error codes:
       - 404 unknown department (no employees)
       - 422 if to < from
       - 422 if range exceeds 92 days
       - 422 for invalid date format or missing params
  T02: Normal trend output across multiple periods & exact chronological ordering
  T03: Gap filling via $densify (exactly one row per calendar day, including weekends and zero-log days)
  T04: Department filtering (isolation between departments)
  T05: Headcount & mid-period joiners (headcount changes dynamically from joined_on, includes zero-log employees)
  T06: Half-days, absent/leave statuses, and weekend records (presence status on weekends, null rate on weekends)
  T07: Missing legacy fields (legacy records with missing half_day/late_minutes handled gracefully)
  T08: Zero denominators and empty days (rate is 0.0 on working days with 0 records; null when headcount=0 or weekend)
  T09: 7-day moving average calculation (shorter window at start, skips nulls, 4 decimals half-up)
  T10: Single-day range (from == to) and date boundaries (exact 92-day range)
  T11: Rounding precision (R8: 4 decimals half-up for rates)
"""

import os
import sys
from datetime import datetime, date, timedelta, timezone

import requests

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

BASE = "http://localhost:8000"


def get_db():
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    return client[os.getenv("MONGO_DB", "attendance_db")]


def cleanup_test_data(db):
    db.employees.delete_many({"emp_code": {"$regex": "^EMP94"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP94"}})


def test_trend_suite():
    db = get_db()
    cleanup_test_data(db)

    print("=" * 70)
    print("  RUNNING DEPARTMENT TREND ANALYTICS (STEP 8) TEST SUITE")
    print("=" * 70)

    # ------------------------------------------------------------------ #
    # Setup test employees and departments                               #
    # ------------------------------------------------------------------ #
    # DeptTrendA:
    #   EMP940001: Joined 2026-06-01 (Employed before trend range)
    #   EMP940002: Joined 2026-07-08 (Mid-period joiner: joined on Wednesday Jul 8)
    #   EMP940003: Joined 2026-06-01 (Zero attendance logs employee)
    #
    # DeptTrendB:
    #   EMP940004: Joined 2026-06-01 (Employed in separate department)
    #
    # DeptFutureJoinersOnly:
    #   EMP940005: Joined 2026-08-01 (Future employee -> 0 headcount in July)
    employees = [
        {"emp_code": "EMP940001", "name": "Trend Emp 1", "email": "t1@test.com", "department": "DeptTrendA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-06-01"},
        {"emp_code": "EMP940002", "name": "Trend Emp 2", "email": "t2@test.com", "department": "DeptTrendA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-08"},
        {"emp_code": "EMP940003", "name": "Trend Emp 3 (No Logs)", "email": "t3@test.com", "department": "DeptTrendA", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-06-01"},
        {"emp_code": "EMP940004", "name": "Trend Emp 4 (Other Dept)", "email": "t4@test.com", "department": "DeptTrendB", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-06-01"},
        {"emp_code": "EMP940005", "name": "Trend Emp 5 (Future)", "email": "t5@test.com", "department": "DeptFutureJoinersOnly", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01"},
    ]
    for emp in employees:
        r = requests.post(f"{BASE}/employees", json=emp)
        assert r.status_code == 201, f"Failed to create {emp['emp_code']}: {r.text}"

    # ------------------------------------------------------------------ #
    # T01: Validation and HTTP error codes                               #
    # ------------------------------------------------------------------ #
    print("\n[T01] Testing parameter validation and HTTP error codes...")
    # Unknown department -> 404
    r = requests.get(f"{BASE}/analytics/departments/UnknownDept123/trend", params={"from": "2026-07-01", "to": "2026-07-10"})
    assert r.status_code == 404, f"Expected 404 for unknown department, got {r.status_code}"

    # Missing from / to -> 422
    r = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"to": "2026-07-10"})
    assert r.status_code == 422, f"Expected 422 for missing from, got {r.status_code}"
    r = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"from": "2026-07-01"})
    assert r.status_code == 422, f"Expected 422 for missing to, got {r.status_code}"

    # Invalid date formats
    for bad_d in ["invalid", "2026-13-01", "2026/07/01", "2026-02-30", "2026-7-1"]:
        r = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"from": bad_d, "to": "2026-07-10"})
        assert r.status_code == 422, f"Expected 422 for invalid date '{bad_d}', got {r.status_code}"

    # to < from -> 422
    r = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"from": "2026-07-10", "to": "2026-07-01"})
    assert r.status_code == 422, f"Expected 422 when to < from, got {r.status_code}"

    # Range exceeds 92 days -> 422 (e.g. 100 days)
    r = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"from": "2026-01-01", "to": "2026-04-15"})
    assert r.status_code == 422, f"Expected 422 when range exceeds 92 days, got {r.status_code}"
    print("  PASS: 404 on unknown department, 422 on invalid dates, to < from, and range > 92 days.")

    # ------------------------------------------------------------------ #
    # Insert attendance logs for DeptTrendA                              #
    # ------------------------------------------------------------------ #
    # Range tested: 2026-07-05 (Sun) to 2026-07-15 (Wed) [11 calendar days]
    #
    # EMP940001 (DeptTrendA):
    #   - 2026-07-06 (Mon): PRESENT, work_hours=8.0, late=0
    #   - 2026-07-07 (Tue): Half-day (WFH, 3.5h, half_day=True) -> present_count = 0.5
    #   - 2026-07-08 (Wed): ABSENT -> does not count as present
    #   - 2026-07-09 (Thu): LEAVE -> does not count as present
    #   - 2026-07-10 (Fri): ON_DUTY, late_minutes=15 -> present_count = 1.0, late_count = 1
    #   - 2026-07-11 (Sat): Weekend PRESENT, work_hours=6.0, late_minutes=25 -> present_count = 1.0, late_count = 1
    #   - 2026-07-12 (Sun): (no log)
    #   - 2026-07-13 (Mon): Legacy record (missing half_day, missing late_minutes) -> present_count = 1.0, late_count = 0
    #   - 2026-07-14 (Tue): (no log)
    #   - 2026-07-15 (Wed): PRESENT, work_hours=8.0, late=0 -> present_count = 1.0
    #
    # EMP940002 (DeptTrendA, joined 2026-07-08):
    #   - 2026-07-08 (Wed): PRESENT, work_hours=8.0 -> present_count = 1.0
    #   - 2026-07-09 (Thu): (no log)
    #   - 2026-07-10 (Fri): PRESENT, work_hours=8.0 -> present_count = 1.0
    #
    # EMP940003 (DeptTrendA): (zero logs)
    #
    # EMP940004 (DeptTrendB):
    #   - 2026-07-06 (Mon): PRESENT, late_minutes=45 -> in DeptTrendB, must NOT affect DeptTrendA!
    logs = [
        {"emp_code": "EMP940001", "date": "2026-07-06", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP940001", "date": "2026-07-07", "status": "WFH", "work_hours": 3.5, "late_minutes": 0, "half_day": True},
        {"emp_code": "EMP940001", "date": "2026-07-08", "status": "ABSENT", "work_hours": None, "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP940001", "date": "2026-07-09", "status": "LEAVE", "work_hours": None, "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP940001", "date": "2026-07-10", "status": "ON_DUTY", "work_hours": 8.0, "late_minutes": 15, "half_day": False},
        {"emp_code": "EMP940001", "date": "2026-07-11", "status": "PRESENT", "work_hours": 6.0, "late_minutes": 25, "half_day": False},
        {"emp_code": "EMP940001", "date": "2026-07-13", "status": "PRESENT", "work_hours": 8.0}, # legacy: missing half_day & late_minutes
        {"emp_code": "EMP940001", "date": "2026-07-15", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "half_day": False},

        {"emp_code": "EMP940002", "date": "2026-07-08", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "half_day": False},
        {"emp_code": "EMP940002", "date": "2026-07-10", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 0, "half_day": False},

        {"emp_code": "EMP940004", "date": "2026-07-06", "status": "PRESENT", "work_hours": 8.0, "late_minutes": 45, "half_day": False},
    ]
    db.attendance_logs.insert_many(logs)

    # ------------------------------------------------------------------ #
    # T02 & T03: Normal trend output, gap-filling, chronological order   #
    # ------------------------------------------------------------------ #
    print("\n[T02 & T03] Testing gap-filling ($densify), ordering, and row counts...")
    r = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"from": "2026-07-05", "to": "2026-07-15"})
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["department"] == "DeptTrendA"
    items = body["items"]

    # 11 days inclusive: 2026-07-05 to 2026-07-15
    assert len(items) == 11, f"Expected 11 rows for 11 days, got {len(items)}"

    # Verify chronological ordering
    expected_dates = [(date(2026, 7, 5) + timedelta(days=i)).isoformat() for i in range(11)]
    actual_dates = [it["date"] for it in items]
    assert actual_dates == expected_dates, f"Dates not exact chronological sequence: {actual_dates}"
    print("  PASS: Exactly 11 calendar days returned in strict chronological order.")

    # ------------------------------------------------------------------ #
    # T04 & T05: Department filtering & Headcount dynamics               #
    # ------------------------------------------------------------------ #
    print("\n[T04 & T05] Testing department isolation and dynamic headcount...")
    # DeptTrendA employees:
    # EMP940001 (joined 2026-06-01) -> present in headcount from Jul 5
    # EMP940003 (joined 2026-06-01, zero logs) -> present in headcount from Jul 5
    # EMP940002 (joined 2026-07-08) -> joins on Jul 8
    #
    # Therefore:
    # Jul 5 to Jul 7: headcount = 2 (EMP940001 + EMP940003)
    # Jul 8 to Jul 15: headcount = 3 (EMP940001 + EMP940002 + EMP940003)
    row_map = {it["date"]: it for it in items}

    for d in ["2026-07-05", "2026-07-06", "2026-07-07"]:
        assert row_map[d]["headcount"] == 2, f"Expected headcount 2 on {d}, got {row_map[d]['headcount']}"

    for d in ["2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13", "2026-07-14", "2026-07-15"]:
        assert row_map[d]["headcount"] == 3, f"Expected headcount 3 on {d}, got {row_map[d]['headcount']}"

    # Verify department isolation: EMP940004 in DeptTrendB was late on Jul 6, but DeptTrendA late_count must be 0!
    assert row_map["2026-07-06"]["late_count"] == 0, "DeptTrendB's late count leaked into DeptTrendA"
    print("  PASS: Headcount dynamically transitions from 2 to 3 on joining date; department isolated.")

    # ------------------------------------------------------------------ #
    # T06: Statuses, half-days, and weekend records                      #
    # ------------------------------------------------------------------ #
    print("\n[T06] Testing presence, half-day, ABSENT/LEAVE, and weekend behavior...")
    # 2026-07-06 (Mon): 1 PRESENT -> present_count = 1.0, late_count = 0, is_working_day = True
    assert row_map["2026-07-06"]["is_working_day"] is True
    assert row_map["2026-07-06"]["present_count"] == 1.0
    assert row_map["2026-07-06"]["attendance_rate"] == 0.5  # 1.0 / 2 = 0.5

    # 2026-07-07 (Tue): 1 Half-day WFH -> present_count = 0.5, rate = 0.5 / 2 = 0.25
    assert row_map["2026-07-07"]["present_count"] == 0.5
    assert row_map["2026-07-07"]["attendance_rate"] == 0.25

    # 2026-07-08 (Wed): EMP940001 is ABSENT (0), EMP940002 is PRESENT (1.0) -> present_count = 1.0
    # Headcount = 3 -> rate = 1.0 / 3 = 0.33333... -> half-up rounds to 0.3333
    assert row_map["2026-07-08"]["present_count"] == 1.0
    assert row_map["2026-07-08"]["attendance_rate"] == 0.3333

    # 2026-07-09 (Thu): EMP940001 is LEAVE (0), EMP940002 has no log (0) -> present_count = 0.0
    # Rate = 0.0 / 3 = 0.0 (A working day with no records has rate 0.0)
    assert row_map["2026-07-09"]["present_count"] == 0.0
    assert row_map["2026-07-09"]["attendance_rate"] == 0.0

    # 2026-07-10 (Fri): EMP940001 ON_DUTY late 15min (1.0), EMP940002 PRESENT (1.0)
    # present_count = 2.0, late_count = 1, rate = 2.0 / 3 = 0.66666... -> 0.6667
    assert row_map["2026-07-10"]["present_count"] == 2.0
    assert row_map["2026-07-10"]["late_count"] == 1
    assert row_map["2026-07-10"]["attendance_rate"] == 0.6667

    # 2026-07-11 (Sat): Weekend PRESENT with late=25
    # present_count = 1.0 (counted on weekend per contract: 'any weekday or weekend')
    # late_count = 1
    # is_working_day = False
    # attendance_rate = null (only on working days with headcount > 0)
    assert row_map["2026-07-11"]["is_working_day"] is False
    assert row_map["2026-07-11"]["present_count"] == 1.0
    assert row_map["2026-07-11"]["late_count"] == 1
    assert row_map["2026-07-11"]["attendance_rate"] is None
    print("  PASS: Half-day (0.5), LEAVE/ABSENT (0), and weekend records handled exactly.")

    # ------------------------------------------------------------------ #
    # T07: Legacy records with missing fields                            #
    # ------------------------------------------------------------------ #
    print("\n[T07] Testing legacy records with missing fields...")
    # 2026-07-13 (Mon): EMP940001 legacy record (missing half_day, missing late_minutes)
    # Treated as half_day=False (1.0 present_count), late_minutes=0
    assert row_map["2026-07-13"]["present_count"] == 1.0
    assert row_map["2026-07-13"]["late_count"] == 0
    assert row_map["2026-07-13"]["attendance_rate"] == 0.3333  # 1.0 / 3
    print("  PASS: Legacy record with missing half_day and late_minutes defaulted correctly.")

    # ------------------------------------------------------------------ #
    # T08: Zero denominators and future-joiner departments               #
    # ------------------------------------------------------------------ #
    print("\n[T08] Testing zero denominators & headcount=0 days...")
    # DeptFutureJoinersOnly: EMP940005 joined 2026-08-01.
    # Query July: Department exists (so not 404), but headcount is 0 every day.
    # attendance_rate must be null, moving_avg_7d must be null.
    r_fut = requests.get(f"{BASE}/analytics/departments/DeptFutureJoinersOnly/trend", params={"from": "2026-07-01", "to": "2026-07-05"})
    assert r_fut.status_code == 200
    fut_items = r_fut.json()["items"]
    assert len(fut_items) == 5
    for item in fut_items:
        assert item["headcount"] == 0
        assert item["present_count"] == 0.0
        assert item["attendance_rate"] is None, f"Expected null rate when headcount=0, got {item['attendance_rate']}"
        assert item["moving_avg_7d"] is None, f"Expected null moving_avg when all rates are null, got {item['moving_avg_7d']}"
    print("  PASS: Department with headcount=0 yields null attendance_rate and null moving_avg_7d.")

    # ------------------------------------------------------------------ #
    # T09: 7-day moving average calculation & shorter window at start    #
    # ------------------------------------------------------------------ #
    print("\n[T09] Testing 7-day moving average window and null-rate handling...")
    # Day 0: 2026-07-05 (Sun): rate = null -> moving_avg_7d = null
    assert row_map["2026-07-05"]["moving_avg_7d"] is None

    # Day 1: 2026-07-06 (Mon): rates in window [null, 0.5] -> mean of non-nulls = 0.5
    assert row_map["2026-07-06"]["moving_avg_7d"] == 0.5

    # Day 2: 2026-07-07 (Tue): rates in window [null, 0.5, 0.25] -> mean = (0.5 + 0.25) / 2 = 0.375
    assert row_map["2026-07-07"]["moving_avg_7d"] == 0.375

    # Day 6: 2026-07-11 (Sat): rate is null.
    # Window covers 7 days (Jul 5 to Jul 11):
    # Rates: Jul 5 (null), Jul 6 (0.5), Jul 7 (0.25), Jul 8 (0.333333...), Jul 9 (0.0), Jul 10 (0.666666...), Jul 11 (null)
    # Non-null sum = 0.5 + 0.25 + 1/3 + 0 + 2/3 = 1.75
    # Non-null count = 5
    # Mean = 1.75 / 5 = 0.35
    assert row_map["2026-07-11"]["moving_avg_7d"] == 0.35
    print("  PASS: Moving average skips nulls, starts with shorter window, and preserves 4-decimal precision.")

    # ------------------------------------------------------------------ #
    # T10: Single-day range and 92-day boundary                          #
    # ------------------------------------------------------------------ #
    print("\n[T10] Testing single-day range (from == to) and 92-day boundary...")
    # from == to: 1 day range
    r_single = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"from": "2026-07-06", "to": "2026-07-06"})
    assert r_single.status_code == 200
    assert len(r_single.json()["items"]) == 1
    assert r_single.json()["items"][0]["date"] == "2026-07-06"
    assert r_single.json()["items"][0]["present_count"] == 1.0

    # Exact 92-day difference (e.g. 2026-01-01 to 2026-04-03)
    r_92 = requests.get(f"{BASE}/analytics/departments/DeptTrendA/trend", params={"from": "2026-01-01", "to": "2026-04-03"})
    assert r_92.status_code == 200, f"Expected 200 for 92-day range, got {r_92.status_code}"
    print("  PASS: Single-day range and 92-day boundary work as specified.")

    # Cleanup
    cleanup_test_data(db)
    print("\n" + "=" * 70)
    print("  ALL 11 DEPARTMENT TREND ANALYTICS TESTS PASSED!")
    print("=" * 70)


if __name__ == "__main__":
    test_trend_suite()
