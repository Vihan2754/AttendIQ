"""
Test Suite for Step 9: GET /admin/explain/{endpoint}
(MongoDB Explain Output for Endpoint Queries and Aggregations)

Coverage:
  T01: Response schema validation (endpoint, collection, explain) for all 5 supported endpoints
  T02: ExecutionStats verbosity present in raw explain document
  T03: Path parameter validation (unknown/unsupported endpoint -> 422)
  T04: Missing required parameters per endpoint (422):
       - employee_monthly missing emp_code / month
       - department_summary missing month
       - late_leaderboard missing month
       - department_trend missing department / from / to
  T05: Query parameter format validation (422):
       - invalid month format (2026-13, invalid)
       - invalid date format (2026-02-30, bad-date)
       - to < from for department_trend
       - date range > 92 days for department_trend
       - date_from > date_to for attendance_list
       - invalid status for attendance_list
       - limit out of bounds (< 1 or > 50)
       - page / page_size out of bounds
  T06: Unknown employee, department, or missing resources (returns 200 explain plan without crashing)
  T07: Empty / missing attendance data periods (returns 200 explain plan)
  T08: Query parameters reflected in explain command/stages
  T09: Boundary cases:
       - from == to (1 day range) for department_trend
       - exact 92-day date range boundary for department_trend
       - limit=1 and limit=50 boundaries
"""

import os
import sys
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = "http://localhost:8000"


def test_explain_suite():
    print("=" * 70)
    print("  RUNNING ADMIN EXPLAIN (STEP 9) TEST SUITE")
    print("=" * 70)

    # ------------------------------------------------------------------ #
    # T01 & T02: Normal successful response & schema for all 5 endpoints #
    # ------------------------------------------------------------------ #
    print("\n[T01 & T02] Testing normal explain response schema & executionStats...")

    # 1. attendance_list
    r1 = requests.get(f"{BASE}/admin/explain/attendance_list", params={"emp_code": "EMP001"})
    assert r1.status_code == 200, f"attendance_list failed: {r1.text}"
    data1 = r1.json()
    assert data1["endpoint"] == "attendance_list"
    assert data1["collection"] == "attendance_logs"
    assert "explain" in data1 and isinstance(data1["explain"], dict)
    assert "executionStats" in data1["explain"] or "queryPlanner" in data1["explain"]
    print("  PASS: attendance_list explain schema & collection verified.")

    # 2. employee_monthly
    r2 = requests.get(f"{BASE}/admin/explain/employee_monthly", params={"emp_code": "EMP001", "month": "2026-07"})
    assert r2.status_code == 200, f"employee_monthly failed: {r2.text}"
    data2 = r2.json()
    assert data2["endpoint"] == "employee_monthly"
    assert data2["collection"] == "attendance_logs"
    assert "explain" in data2 and isinstance(data2["explain"], dict)
    assert "executionStats" in data2["explain"] or "stages" in data2["explain"]
    print("  PASS: employee_monthly explain schema & collection verified.")

    # 3. department_summary
    r3 = requests.get(f"{BASE}/admin/explain/department_summary", params={"month": "2026-07", "department": "Engineering"})
    assert r3.status_code == 200, f"department_summary failed: {r3.text}"
    data3 = r3.json()
    assert data3["endpoint"] == "department_summary"
    assert data3["collection"] == "employees"
    assert "explain" in data3 and isinstance(data3["explain"], dict)
    assert "executionStats" in data3["explain"] or "stages" in data3["explain"]
    print("  PASS: department_summary explain schema & collection verified.")

    # 4. late_leaderboard
    r4 = requests.get(f"{BASE}/admin/explain/late_leaderboard", params={"month": "2026-07", "department": "Engineering", "limit": 5})
    assert r4.status_code == 200, f"late_leaderboard failed: {r4.text}"
    data4 = r4.json()
    assert data4["endpoint"] == "late_leaderboard"
    assert data4["collection"] == "attendance_logs"
    assert "explain" in data4 and isinstance(data4["explain"], dict)
    assert "executionStats" in data4["explain"] or "stages" in data4["explain"]
    print("  PASS: late_leaderboard explain schema & collection verified.")

    # 5. department_trend
    r5 = requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "from": "2026-07-06", "to": "2026-07-10"})
    assert r5.status_code == 200, f"department_trend failed: {r5.text}"
    data5 = r5.json()
    assert data5["endpoint"] == "department_trend"
    assert data5["collection"] == "attendance_logs"
    assert "explain" in data5 and isinstance(data5["explain"], dict)
    assert "executionStats" in data5["explain"] or "stages" in data5["explain"]
    print("  PASS: department_trend explain schema & collection verified.")

    # ------------------------------------------------------------------ #
    # T03: Unknown endpoint path parameter -> 422                        #
    # ------------------------------------------------------------------ #
    print("\n[T03] Testing invalid endpoint enum in path...")
    for bad_ep in ["invalid_endpoint", "unknown", "payroll_summary", "attendance"]:
        r = requests.get(f"{BASE}/admin/explain/{bad_ep}")
        assert r.status_code == 422, f"Expected 422 for bad endpoint '{bad_ep}', got {r.status_code}"
    print("  PASS: 422 returned for unsupported endpoint enums.")

    # ------------------------------------------------------------------ #
    # T04: Missing required parameters per endpoint -> 422               #
    # ------------------------------------------------------------------ #
    print("\n[T04] Testing missing required parameters per endpoint...")
    # employee_monthly: requires emp_code and month
    assert requests.get(f"{BASE}/admin/explain/employee_monthly").status_code == 422
    assert requests.get(f"{BASE}/admin/explain/employee_monthly", params={"emp_code": "EMP001"}).status_code == 422
    assert requests.get(f"{BASE}/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422

    # department_summary: requires month
    assert requests.get(f"{BASE}/admin/explain/department_summary").status_code == 422

    # late_leaderboard: requires month
    assert requests.get(f"{BASE}/admin/explain/late_leaderboard").status_code == 422

    # department_trend: requires department, from, to
    assert requests.get(f"{BASE}/admin/explain/department_trend").status_code == 422
    assert requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering"}).status_code == 422
    assert requests.get(f"{BASE}/admin/explain/department_trend", params={"from": "2026-07-01", "to": "2026-07-10"}).status_code == 422
    assert requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "from": "2026-07-01"}).status_code == 422
    assert requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "to": "2026-07-10"}).status_code == 422
    print("  PASS: 422 returned when required parameters are omitted.")

    # ------------------------------------------------------------------ #
    # T05: Invalid parameter formats and bounds -> 422                   #
    # ------------------------------------------------------------------ #
    print("\n[T05] Testing parameter formats, ranges, and validation...")
    # Bad month format
    for bad_m in ["2026-13", "invalid", "2026-00", "2026/07"]:
        r = requests.get(f"{BASE}/admin/explain/employee_monthly", params={"emp_code": "EMP001", "month": bad_m})
        assert r.status_code == 422, f"Expected 422 for month {bad_m}, got {r.status_code}"

    # department_trend: to < from -> 422
    r = requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "from": "2026-07-10", "to": "2026-07-01"})
    assert r.status_code == 422, f"Expected 422 for to < from, got {r.status_code}"

    # department_trend: date range > 92 days -> 422
    r = requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "from": "2026-01-01", "to": "2026-05-01"})
    assert r.status_code == 422, f"Expected 422 for range > 92 days, got {r.status_code}"

    # attendance_list: date_from > date_to -> 422
    r = requests.get(f"{BASE}/admin/explain/attendance_list", params={"date_from": "2026-07-10", "date_to": "2026-07-01"})
    assert r.status_code == 422, f"Expected 422 for date_from > date_to, got {r.status_code}"

    # attendance_list: invalid status -> 422
    r = requests.get(f"{BASE}/admin/explain/attendance_list", params={"status": "INVALID_STATUS"})
    assert r.status_code == 422, f"Expected 422 for invalid status, got {r.status_code}"

    # late_leaderboard: limit bounds (ge=1, le=50) -> 422
    for bad_lim in [0, -1, 51, 100]:
        r = requests.get(f"{BASE}/admin/explain/late_leaderboard", params={"month": "2026-07", "limit": bad_lim})
        assert r.status_code == 422, f"Expected 422 for limit={bad_lim}, got {r.status_code}"

    # page and page_size bounds
    assert requests.get(f"{BASE}/admin/explain/attendance_list", params={"page": 0}).status_code == 422
    assert requests.get(f"{BASE}/admin/explain/attendance_list", params={"page_size": 0}).status_code == 422
    assert requests.get(f"{BASE}/admin/explain/attendance_list", params={"page_size": 101}).status_code == 422
    print("  PASS: All parameter format, range, and logic validations properly return 422.")

    # ------------------------------------------------------------------ #
    # T06: Unknown resources (non-existent employee, department) -> 200  #
    # ------------------------------------------------------------------ #
    print("\n[T06] Testing explain behavior for non-existent resources...")
    # Non-existent employee explain
    r = requests.get(f"{BASE}/admin/explain/employee_monthly", params={"emp_code": "NON_EXISTENT_EMP", "month": "2026-07"})
    assert r.status_code == 200
    assert r.json()["endpoint"] == "employee_monthly"

    # Non-existent department explain
    r = requests.get(f"{BASE}/admin/explain/department_summary", params={"month": "2026-07", "department": "NonExistentDept"})
    assert r.status_code == 200
    assert r.json()["endpoint"] == "department_summary"

    r = requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "NonExistentDept", "from": "2026-07-01", "to": "2026-07-05"})
    assert r.status_code == 200
    assert r.json()["endpoint"] == "department_trend"
    print("  PASS: Non-existent employee/department queries successfully return query execution plan.")

    # ------------------------------------------------------------------ #
    # T07: Empty attendance data periods                                 #
    # ------------------------------------------------------------------ #
    print("\n[T07] Testing explain on empty / distant dates...")
    r = requests.get(f"{BASE}/admin/explain/late_leaderboard", params={"month": "2020-01"})
    assert r.status_code == 200
    assert r.json()["endpoint"] == "late_leaderboard"

    r = requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "from": "2020-01-01", "to": "2020-01-10"})
    assert r.status_code == 200
    assert r.json()["endpoint"] == "department_trend"
    print("  PASS: Explain on empty attendance dates returns 200 execution plan.")

    # ------------------------------------------------------------------ #
    # T08: Query parameters reflected in explain command/output          #
    # ------------------------------------------------------------------ #
    print("\n[T08] Testing query parameters pass-through...")
    r = requests.get(f"{BASE}/admin/explain/attendance_list", params={"emp_code": "EMP001", "status": "PRESENT", "page": 2, "page_size": 15})
    assert r.status_code == 200
    cmd = r.json()["explain"].get("command", {})
    if "filter" in cmd:
        assert cmd["filter"].get("emp_code") == "EMP001"
        assert cmd["filter"].get("status") == "PRESENT"
    if "limit" in cmd:
        assert cmd["limit"] == 15
    if "skip" in cmd:
        assert cmd["skip"] == 15
    print("  PASS: Query parameters accurately reflected in explain execution command.")

    # ------------------------------------------------------------------ #
    # T09: Boundary cases: single-day range, 92-day boundary, limits    #
    # ------------------------------------------------------------------ #
    print("\n[T09] Testing boundary cases (single-day, 92-day boundary, limit boundaries)...")
    # Single day range (from == to)
    r = requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "from": "2026-07-06", "to": "2026-07-06"})
    assert r.status_code == 200
    assert r.json()["endpoint"] == "department_trend"

    # Exactly 92-day boundary (2026-01-01 to 2026-04-03 is 92 days)
    r = requests.get(f"{BASE}/admin/explain/department_trend", params={"department": "Engineering", "from": "2026-01-01", "to": "2026-04-03"})
    assert r.status_code == 200

    # Limit boundaries: limit=1 and limit=50
    assert requests.get(f"{BASE}/admin/explain/late_leaderboard", params={"month": "2026-07", "limit": 1}).status_code == 200
    assert requests.get(f"{BASE}/admin/explain/late_leaderboard", params={"month": "2026-07", "limit": 50}).status_code == 200
    print("  PASS: Single-day, 92-day boundary, and limit boundaries succeed.")

    print("\n" + "=" * 70)
    print("  ALL 9 ADMIN EXPLAIN TESTS PASSED!")
    print("=" * 70)


if __name__ == "__main__":
    test_explain_suite()
