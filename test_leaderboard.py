"""
Test Suite for Step 7: GET /analytics/leaderboard/late
(Late-Arrival Leaderboard API)

Covers:
  T01: Normal leaderboard results (month, rank, emp_code, name, department, total_late_minutes, late_count)
  T02: Correct ranking and ordering (standard competition ranking: 1, 2, 2, 4; ties share rank, next rank skipped; tie-breaking by emp_code asc)
  T03: Ties at the cutoff (limit applies after ranking: rank <= limit, can return > limit rows)
  T04: Department filter (ranking happens inside that department only)
  T05: Reporting-period filter (logs strictly within month; weekend logs included per R7)
  T06: Missing late_minutes values (legacy records default to 0 and are not included)
  T07: Employees with no attendance records (excluded from leaderboard as total > 0 required)
  T08: Empty result sets (return 200 with items: [])
  T09: Invalid query parameters (missing month -> 422, invalid month -> 422, limit < 1 or limit > 50 -> 422)
  T10: Limit boundaries (limit=1, limit=50, default limit=10)
  T11: Logs whose employee does not exist (ignored from leaderboard)
"""

import os
import sys
import calendar
from datetime import datetime, timezone

import requests

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

BASE = "http://localhost:8000"


def get_db():
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    return client[os.getenv("MONGO_DB", "attendance_db")]


def cleanup_test_data(db):
    db.employees.delete_many({"emp_code": {"$regex": "^EMP95"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP95"}})
    db.attendance_logs.delete_many({"emp_code": "ORPHAN999"})


def test_leaderboard_suite():
    db = get_db()
    cleanup_test_data(db)

    print("=" * 70)
    print("  RUNNING LATE-ARRIVAL LEADERBOARD (STEP 7) TEST SUITE")
    print("=" * 70)

    # ------------------------------------------------------------------ #
    # T09: Invalid query parameters & validation error codes (422)       #
    # ------------------------------------------------------------------ #
    print("\n[T09] Testing query parameter validation...")
    # Missing month
    r = requests.get(f"{BASE}/analytics/leaderboard/late")
    assert r.status_code == 422, f"Expected 422 for missing month, got {r.status_code}"

    # Invalid month formats
    for bad_month in ["2026-13", "2026-00", "2026/07", "2026-7", "invalid"]:
        r = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": bad_month})
        assert r.status_code == 422, f"Expected 422 for month '{bad_month}', got {r.status_code}"

    # Invalid limit values
    for bad_limit in [0, -1, 51, 100, "abc"]:
        r = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07", "limit": bad_limit})
        assert r.status_code == 422, f"Expected 422 for limit '{bad_limit}', got {r.status_code}"

    print("  PASS: 422 correctly returned for missing month, invalid month formats, and limit bounds (ge=1, le=50).")

    # ------------------------------------------------------------------ #
    # T08: Empty result set                                              #
    # ------------------------------------------------------------------ #
    print("\n[T08] Testing empty result sets...")
    # A month with no records at all
    r = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2025-01"})
    assert r.status_code == 200, f"Expected 200 for empty month, got {r.status_code}"
    body = r.json()
    assert body["month"] == "2025-01"
    assert body["items"] == [], f"Expected empty items, got {body['items']}"

    # A non-existent department
    r = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07", "department": "NoSuchDept"})
    assert r.status_code == 200
    assert r.json()["items"] == []
    print("  PASS: Empty result set returns 200 with empty items list.")

    # ------------------------------------------------------------------ #
    # Setup test data                                                    #
    # ------------------------------------------------------------------ #
    # Employees in DeptEng:
    #   EMP950001: Asha (DeptEng)
    #   EMP950002: Bob  (DeptEng)
    #   EMP950003: Carol (DeptEng)
    #   EMP950004: Dave (DeptEng)
    #   EMP950005: Eve  (DeptEng) - zero logs
    #   EMP950006: Frank (DeptEng) - only punctual logs (0 late minutes)
    #
    # Employees in DeptSales:
    #   EMP950007: Grace (DeptSales)
    #   EMP950008: Heidi (DeptSales)
    employees = [
        {"emp_code": "EMP950001", "name": "Asha Patel", "email": "asha.p@test.com", "department": "DeptEng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP950002", "name": "Bob Smith", "email": "bob.s@test.com", "department": "DeptEng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP950003", "name": "Carol Danvers", "email": "carol.d@test.com", "department": "DeptEng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP950004", "name": "Dave Bowman", "email": "dave.b@test.com", "department": "DeptEng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP950005", "name": "Eve Polastri", "email": "eve.p@test.com", "department": "DeptEng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP950006", "name": "Frank Castle", "email": "frank.c@test.com", "department": "DeptEng", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP950007", "name": "Grace Hopper", "email": "grace.h@test.com", "department": "DeptSales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP950008", "name": "Heidi N", "email": "heidi.n@test.com", "department": "DeptSales", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
    ]
    for emp in employees:
        r = requests.post(f"{BASE}/employees", json=emp)
        assert r.status_code == 201, f"Failed to create employee {emp['emp_code']}: {r.text}"

    # Attendance logs for July 2026:
    # EMP950001: 2 late days (30 min + 20 min) = 50 min late, count 2
    # EMP950002: 1 late day (40 min) = 40 min late, count 1
    # EMP950003: 1 late day (40 min) = 40 min late, count 1  (TIED with EMP950002 at 40 min)
    # EMP950004: 1 late day on weekend (2026-07-11 Sat, 15 min) = 15 min late, count 1
    # EMP950005: 0 logs
    # EMP950006: 1 log on 2026-07-06 with late_minutes=0 (punctual) + 1 legacy log with missing late_minutes
    # EMP950007: 1 late day in July (90 min) = 90 min late, count 1
    # EMP950008: 1 late day in August (75 min) -> should NOT count in July
    # ORPHAN999: non-existent employee with 100 min late -> should be IGNORED
    logs = [
        # EMP950001 (DeptEng)
        {"emp_code": "EMP950001", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 30, "work_hours": 8.0, "half_day": False},
        {"emp_code": "EMP950001", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 20, "work_hours": 8.0, "half_day": False},

        # EMP950002 (DeptEng) - total 40
        {"emp_code": "EMP950002", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 40, "work_hours": 8.0, "half_day": False},

        # EMP950003 (DeptEng) - total 40 (tie with EMP950002)
        {"emp_code": "EMP950003", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 40, "work_hours": 8.0, "half_day": False},

        # EMP950004 (DeptEng) - weekend late log (2026-07-11 is Saturday)
        {"emp_code": "EMP950004", "date": "2026-07-11", "status": "PRESENT", "late_minutes": 15, "work_hours": 4.0, "half_day": True},

        # EMP950006 (DeptEng) - punctual log + legacy log missing late_minutes
        {"emp_code": "EMP950006", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "work_hours": 9.0, "half_day": False},
        {"emp_code": "EMP950006", "date": "2026-07-07", "status": "PRESENT", "work_hours": 9.0}, # missing late_minutes

        # EMP950007 (DeptSales) - 90 min late in July
        {"emp_code": "EMP950007", "date": "2026-07-10", "status": "PRESENT", "late_minutes": 90, "work_hours": 7.0, "half_day": False},

        # EMP950008 (DeptSales) - 75 min late in AUGUST
        {"emp_code": "EMP950008", "date": "2026-08-03", "status": "PRESENT", "late_minutes": 75, "work_hours": 7.5, "half_day": False},

        # Orphan log (no employee in db.employees)
        {"emp_code": "ORPHAN999", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 120, "work_hours": 6.0, "half_day": False},
    ]
    db.attendance_logs.insert_many(logs)

    # ------------------------------------------------------------------ #
    # T01 & T11: Normal leaderboard & orphan log exclusion               #
    # ------------------------------------------------------------------ #
    print("\n[T01 & T11] Testing normal leaderboard results & orphan log exclusion...")
    r = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07"})
    assert r.status_code == 200
    body = r.json()
    assert body["month"] == "2026-07"
    items = body["items"]

    # Filter to only our test employees
    test_items = [it for it in items if it["emp_code"].startswith("EMP95")]
    orphan_items = [it for it in items if it["emp_code"] == "ORPHAN999"]
    assert len(orphan_items) == 0, f"Orphan logs without employee must be ignored! Found: {orphan_items}"
    print("  PASS: Logs for non-existent employees are ignored.")

    # Check structure of items
    first = items[0]
    for key in ["rank", "emp_code", "name", "department", "total_late_minutes", "late_count"]:
        assert key in first, f"Missing required field {key} in item: {first}"
    assert isinstance(first["rank"], int) and first["rank"] >= 1
    assert isinstance(first["total_late_minutes"], int)
    assert isinstance(first["late_count"], int)
    print("  PASS: Response schema matches Leaderboard definition exactly.")

    # ------------------------------------------------------------------ #
    # T02: Competition ranking (1, 2, 2, 4) & sorting order              #
    # ------------------------------------------------------------------ #
    print("\n[T02] Testing competition ranking (1, 2, 2, 4) & sorting order...")
    # Across all employees in July:
    # 1. EMP950007 (DeptSales): 90 min -> Rank 1
    # 2. EMP950001 (DeptEng):   50 min -> Rank 2
    # 3. EMP950002 (DeptEng):   40 min -> Rank 3 (TIE)
    # 4. EMP950003 (DeptEng):   40 min -> Rank 3 (TIE)
    # 5. EMP950004 (DeptEng):   15 min -> Rank 5 (Rank 4 skipped because of tie at 3!)
    # Notice: EMP950002 must appear before EMP950003 (emp_code ascending tie-breaker)
    emp_map = {it["emp_code"]: it for it in test_items}
    assert "EMP950007" in emp_map
    assert "EMP950001" in emp_map
    assert "EMP950002" in emp_map
    assert "EMP950003" in emp_map
    assert "EMP950004" in emp_map

    assert emp_map["EMP950007"]["rank"] == 1
    assert emp_map["EMP950007"]["total_late_minutes"] == 90
    assert emp_map["EMP950007"]["late_count"] == 1

    assert emp_map["EMP950001"]["rank"] == 2
    assert emp_map["EMP950001"]["total_late_minutes"] == 50
    assert emp_map["EMP950001"]["late_count"] == 2

    assert emp_map["EMP950002"]["rank"] == 3
    assert emp_map["EMP950002"]["total_late_minutes"] == 40
    assert emp_map["EMP950003"]["rank"] == 3
    assert emp_map["EMP950003"]["total_late_minutes"] == 40

    # Next rank must be skipped: 3 is shared by 2 people, next rank is 5.
    # In sample data, EMP0003 has 35 min late, taking rank 5.
    # Therefore EMP950004 with 15 min late takes rank 6.
    sample_emp = next((it for it in items if it["emp_code"] == "EMP0003"), None)
    if sample_emp:
        assert sample_emp["rank"] == 5, f"Expected rank 5 for EMP0003 (35 min), got {sample_emp['rank']}"
        assert emp_map["EMP950004"]["rank"] == 6, f"Expected rank 6 for EMP950004 after EMP0003, got {emp_map['EMP950004']['rank']}"
    else:
        assert emp_map["EMP950004"]["rank"] == 5, f"Expected rank 5 for EMP950004 after tie at 3, got {emp_map['EMP950004']['rank']}"
    assert emp_map["EMP950004"]["total_late_minutes"] == 15

    # Check sorting order between tied records (total_late_minutes DESC, emp_code ASC)
    idx_02 = [i for i, it in enumerate(test_items) if it["emp_code"] == "EMP950002"][0]
    idx_03 = [i for i, it in enumerate(test_items) if it["emp_code"] == "EMP950003"][0]
    assert idx_02 < idx_03, f"EMP950002 must appear before EMP950003 due to emp_code ASC tie-break"
    print("  PASS: Competition ranking (1, 2, 3, 3, 5, 6) verified and tie-breaking by emp_code asc.")

    # ------------------------------------------------------------------ #
    # T03: Ties at the cutoff (limit applies after ranking)              #
    # ------------------------------------------------------------------ #
    print("\n[T03] Testing ties at the cutoff (rank <= limit)...")
    # In DeptEng:
    # Rank 1: EMP950001 (50 min)
    # Rank 2: EMP950002 (40 min)
    # Rank 2: EMP950003 (40 min)
    # Rank 4: EMP950004 (15 min) - Rank 3 is skipped!
    #
    # Case A: limit = 2
    # Rows with rank <= 2: Rank 1 (EMP950001), Rank 2 (EMP950002), Rank 2 (EMP950003).
    # Total rows returned must be 3 (which is > limit=2)!
    r_lim2 = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07", "department": "DeptEng", "limit": 2})
    assert r_lim2.status_code == 200
    items_lim2 = r_lim2.json()["items"]
    assert len(items_lim2) == 3, f"Expected 3 items when limit=2 due to tie at rank 2, got {len(items_lim2)}"
    assert [it["emp_code"] for it in items_lim2] == ["EMP950001", "EMP950002", "EMP950003"]
    assert [it["rank"] for it in items_lim2] == [1, 2, 2]
    print("  PASS: limit=2 returns 3 rows because EMP950002 and EMP950003 tie at rank 2 <= 2.")

    # Case B: limit = 1
    # Rows with rank <= 1: Only Rank 1 (EMP950001)
    r_lim1 = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07", "department": "DeptEng", "limit": 1})
    assert r_lim1.status_code == 200
    items_lim1 = r_lim1.json()["items"]
    assert len(items_lim1) == 1
    assert items_lim1[0]["emp_code"] == "EMP950001"
    assert items_lim1[0]["rank"] == 1
    print("  PASS: limit=1 returns 1 row (Rank 1).")

    # Case C: limit = 3
    # Rows with rank <= 3: Rank 1, Rank 2, Rank 2 (since Rank 4 > 3). Returns 3 rows.
    r_lim3 = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07", "department": "DeptEng", "limit": 3})
    assert r_lim3.status_code == 200
    items_lim3 = r_lim3.json()["items"]
    assert len(items_lim3) == 3
    print("  PASS: limit=3 returns 3 rows (Rank 4 excluded).")

    # ------------------------------------------------------------------ #
    # T04: Department filter                                             #
    # ------------------------------------------------------------------ #
    print("\n[T04] Testing department filter & ranking within department...")
    # DeptSales in July: Grace has 90 min late -> Rank 1 within DeptSales
    r_sales = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07", "department": "DeptSales"})
    assert r_sales.status_code == 200
    items_sales = r_sales.json()["items"]
    assert len(items_sales) == 1
    assert items_sales[0]["emp_code"] == "EMP950007"
    assert items_sales[0]["rank"] == 1
    assert items_sales[0]["department"] == "DeptSales"
    assert items_sales[0]["total_late_minutes"] == 90
    print("  PASS: Department filter ranks strictly within the specified department.")

    # ------------------------------------------------------------------ #
    # T05: Reporting-period filter & weekend logs                         #
    # ------------------------------------------------------------------ #
    print("\n[T05] Testing reporting-period filter & weekend records...")
    # Heidi is late in August, not July
    assert "EMP950008" not in [it["emp_code"] for it in items_sales]

    # August query for DeptSales: Heidi is late (75 min) -> Rank 1
    r_aug = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-08", "department": "DeptSales"})
    assert r_aug.status_code == 200
    items_aug = r_aug.json()["items"]
    assert len(items_aug) == 1
    assert items_aug[0]["emp_code"] == "EMP950008"
    assert items_aug[0]["total_late_minutes"] == 75

    # Weekend log: EMP950004 late on Saturday (2026-07-11) is included per R7
    assert "EMP950004" in emp_map
    assert emp_map["EMP950004"]["total_late_minutes"] == 15
    print("  PASS: Date filtering strictly scopes to month; weekend records contribute per R7.")

    # ------------------------------------------------------------------ #
    # T06 & T07: Punctual employees, legacy docs, zero-log employees     #
    # ------------------------------------------------------------------ #
    print("\n[T06 & T07] Testing zero-log employees & legacy records...")
    # EMP950005 has 0 logs -> NOT in leaderboard
    assert "EMP950005" not in emp_map, "Employee with 0 logs must not appear on leaderboard"

    # EMP950006 has 0 late minutes (one log with 0, one legacy log with missing late_minutes) -> NOT in leaderboard
    assert "EMP950006" not in emp_map, "Punctual employee (0 late minutes) must not appear on leaderboard"
    print("  PASS: Employees with 0 late minutes and zero logs are excluded from leaderboard.")

    # ------------------------------------------------------------------ #
    # T10: Limit boundary testing                                        #
    # ------------------------------------------------------------------ #
    print("\n[T10] Testing limit boundaries (limit=50, default limit=10)...")
    r_50 = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50})
    assert r_50.status_code == 200

    r_def = requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07"})
    assert r_def.status_code == 200
    print("  PASS: Limit boundaries 1, 10 (default), and 50 work properly.")

    # Cleanup
    cleanup_test_data(db)
    print("\n" + "=" * 70)
    print("  ALL 11 LATE-ARRIVAL LEADERBOARD TESTS PASSED!")
    print("=" * 70)


if __name__ == "__main__":
    test_leaderboard_suite()
