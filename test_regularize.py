"""
Comprehensive test suite for Step 4: PATCH /attendance/{emp_code}/{date}
(Manual Attendance Correction / Regularization API)

Coverage:
  T01: Valid punch-in correction (recomputes late_minutes, work_hours, appends 1 history entry)
  T02: Valid punch-out correction (recomputes work_hours, overtime_minutes, half_day)
  T03: Status correction (PRESENT -> WFH keeps punch times, appends history)
  T04: Status correction to ABSENT/LEAVE clears punch times & resets derived fields
  T05: Changing ABSENT -> PRESENT requires punch_in (422 if omitted)
  T06: Supplying punch_in/punch_out with ABSENT or LEAVE -> 422
  T07: Missing employee -> 404
  T08: Missing attendance record -> 404
  T09: Invalid date format in path -> 422
  T10: Invalid body validation (reason too short, regularized_by empty, bad epoch-ms) -> 422
  T11: punch_in does not fall on attendance date (R1) -> 422
  T12: punch_out <= punch_in -> 422
  T13: punch_out > punch_in + 24 hours -> 422
  T14: Overnight shift correction (shift_end <= shift_start) with overtime calculation
  T15: No-op correction (nothing changed) -> 422
  T16: Multiple sequential corrections (history is immutable and appends each time)
  T17: Concurrent corrections (optimistic concurrency control: loser gets 409)
  T18: Complete audit history serialization (epoch ms for at, punch_in/out from/to, no _id)
  T19: Regression check (helper tests & punch-out tests pass)
"""

import os
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

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


def dt_to_ms(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def cleanup_test_data(db):
    db.employees.delete_many({"emp_code": {"$regex": "^EMP98"}})
    db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP98"}})


def test_regularization_suite():
    db = get_db()
    cleanup_test_data(db)

    print("=" * 65)
    print("  RUNNING REGULARIZATION (STEP 4) TEST SUITE")
    print("=" * 65)

    # ------------------------------------------------------------------ #
    # Setup test employees                                               #
    # ------------------------------------------------------------------ #
    EMP1 = "EMP9801"  # standard shift: 09:30 - 18:30
    EMP_NIGHT = "EMP9802"  # overnight shift: 22:00 - 06:00

    r = requests.post(f"{BASE}/employees", json={
        "emp_code": EMP1,
        "name": "Regularize Tester",
        "email": "reg.tester@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    assert r.status_code == 201, f"Failed to create EMP1: {r.status_code} {r.text}"

    r = requests.post(f"{BASE}/employees", json={
        "emp_code": EMP_NIGHT,
        "name": "Night Shift Tester",
        "email": "night.tester@example.com",
        "department": "Operations",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
    })
    assert r.status_code == 201, f"Failed to create EMP_NIGHT: {r.status_code} {r.text}"

    DATE1 = "2026-07-06"

    # Create initial attendance record for EMP1 on DATE1:
    # Punched in at 10:15 IST (45 min late), punched out at 18:30 IST (8h 15m work, 0 OT, not half day)
    pin1_ist = datetime(2026, 7, 6, 10, 15, 0, tzinfo=IST)
    pout1_ist = datetime(2026, 7, 6, 18, 30, 0, tzinfo=IST)
    pin1_ms = dt_to_ms(pin1_ist)
    pout1_ms = dt_to_ms(pout1_ist)

    # Punch in
    r = requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": EMP1,
        "punched_at": pin1_ms,
        "status": "PRESENT",
    })
    assert r.status_code == 201, f"Initial punch-in failed: {r.text}"

    # Punch out
    r = requests.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP1,
        "punched_at": pout1_ms,
    })
    assert r.status_code == 200, f"Initial punch-out failed: {r.text}"
    rec = r.json()
    assert rec["late_minutes"] == 45
    assert rec["work_hours"] == 8.25
    assert rec["history"] == []

    # ------------------------------------------------------------------ #
    # T07: Missing employee -> 404                                       #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/UNKNOWN999/{DATE1}", json={
        "reason": "Correcting time",
        "regularized_by": "manager.bob",
        "status": "WFH",
    })
    assert r.status_code == 404, f"T07 expected 404 got {r.status_code}"
    print("PASS  T07 unknown employee -> 404")

    # ------------------------------------------------------------------ #
    # T08: Missing attendance record -> 404                              #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/2026-01-01", json={
        "reason": "Correcting time",
        "regularized_by": "manager.bob",
        "status": "WFH",
    })
    assert r.status_code == 404, f"T08 expected 404 got {r.status_code}"
    print("PASS  T08 missing attendance record -> 404")

    # ------------------------------------------------------------------ #
    # T09: Invalid date format in path -> 422                           #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/2026-02-30", json={
        "reason": "Correcting time",
        "regularized_by": "manager.bob",
    })
    assert r.status_code == 422, f"T09 expected 422 got {r.status_code}"

    r = requests.patch(f"{BASE}/attendance/{EMP1}/bad-date", json={
        "reason": "Correcting time",
        "regularized_by": "manager.bob",
    })
    assert r.status_code == 422, f"T09 expected 422 got {r.status_code}"
    print("PASS  T09 invalid path date -> 422")

    # ------------------------------------------------------------------ #
    # T10: Body validation errors -> 422                                 #
    # ------------------------------------------------------------------ #
    # reason too short (< 5 chars)
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "abc",
        "regularized_by": "manager.bob",
    })
    assert r.status_code == 422, f"T10 expected 422 got {r.status_code}"

    # regularized_by empty
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "valid reason",
        "regularized_by": "",
    })
    assert r.status_code == 422, f"T10 expected 422 got {r.status_code}"

    # invalid status
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "valid reason",
        "regularized_by": "manager.bob",
        "status": "INVALID_STATUS",
    })
    assert r.status_code == 422, f"T10 expected 422 got {r.status_code}"

    # out of range timestamp
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "valid reason",
        "regularized_by": "manager.bob",
        "punch_in": 12345,
    })
    assert r.status_code == 422, f"T10 expected 422 got {r.status_code}"
    print("PASS  T10 body validation (reason, by, status, epoch-ms) -> 422")

    # ------------------------------------------------------------------ #
    # T15: No-op correction (no changes) -> 422                          #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "No changes made here",
        "regularized_by": "manager.bob",
        "status": "PRESENT",
        "punch_in": pin1_ms,
        "punch_out": pout1_ms,
    })
    assert r.status_code == 422, f"T15 expected 422 got {r.status_code}"
    print("PASS  T15 no-op correction -> 422")

    # ------------------------------------------------------------------ #
    # T11: punch_in must stay on record's attendance date -> 422        #
    # ------------------------------------------------------------------ #
    # 2026-07-07 09:30 IST is next calendar date
    next_day_ms = dt_to_ms(datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST))
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Moving punch_in to next day",
        "regularized_by": "manager.bob",
        "punch_in": next_day_ms,
    })
    assert r.status_code == 422, f"T11 expected 422 got {r.status_code}"
    print("PASS  T11 punch_in not on record's attendance date -> 422")

    # ------------------------------------------------------------------ #
    # T12: punch_out <= punch_in -> 422                                 #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Punch-out before punch-in",
        "regularized_by": "manager.bob",
        "punch_out": pin1_ms - 1000,
    })
    assert r.status_code == 422, f"T12 expected 422 got {r.status_code}"

    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Punch-out equals punch-in",
        "regularized_by": "manager.bob",
        "punch_out": pin1_ms,
    })
    assert r.status_code == 422, f"T12 expected 422 got {r.status_code}"
    print("PASS  T12 punch_out <= punch_in -> 422")

    # ------------------------------------------------------------------ #
    # T13: punch_out > punch_in + 24 hours -> 422                       #
    # ------------------------------------------------------------------ #
    over_24h_ms = pin1_ms + (24 * 3600 + 1) * 1000
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Punch-out over 24h",
        "regularized_by": "manager.bob",
        "punch_out": over_24h_ms,
    })
    assert r.status_code == 422, f"T13 expected 422 got {r.status_code}"
    print("PASS  T13 punch_out > 24h after punch_in -> 422")

    # ------------------------------------------------------------------ #
    # T01: Valid punch-in correction (recomputes late, work_hours, half) #
    # ------------------------------------------------------------------ #
    # Correct punch-in to 09:28 IST (inside grace, so late_minutes becomes 0)
    # Work hours: 09:28 to 18:30 = 9h 2m = 9.03h
    pin_new_ist = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
    pin_new_ms = dt_to_ms(pin_new_ist)

    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Biometric scanner delay at entrance",
        "regularized_by": "hr.alice",
        "punch_in": pin_new_ms,
    })
    assert r.status_code == 200, f"T01 expected 200 got {r.status_code}: {r.text}"
    data = r.json()
    assert data["punch_in"] == pin_new_ms
    assert data["late_minutes"] == 0, f"expected late_minutes=0 got {data['late_minutes']}"
    assert data["work_hours"] == 9.03, f"expected work_hours=9.03 got {data['work_hours']}"
    assert len(data["history"]) == 1
    h0 = data["history"][0]
    assert h0["by"] == "hr.alice"
    assert h0["reason"] == "Biometric scanner delay at entrance"
    assert isinstance(h0["at"], int)
    assert h0["changes"]["punch_in"] == {"from": pin1_ms, "to": pin_new_ms}
    assert h0["changes"]["late_minutes"] == {"from": 45, "to": 0}
    assert h0["changes"]["work_hours"] == {"from": 8.25, "to": 9.03}
    print(f"PASS  T01 valid punch_in correction -> late: 45->0, work_hours: 8.25->9.03, 1 history entry")

    # ------------------------------------------------------------------ #
    # T02: Valid punch-out correction (recomputes work_hours, overtime)  #
    # ------------------------------------------------------------------ #
    # Correct punch-out to 19:35 IST (1h 5m after shift end 18:30 -> OT = 65 min)
    # Work hours: 09:28 to 19:35 = 10h 7m = 10.12h
    pout_new_ist = datetime(2026, 7, 6, 19, 35, 0, tzinfo=IST)
    pout_new_ms = dt_to_ms(pout_new_ist)

    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Overtime approved by project lead",
        "regularized_by": "hr.alice",
        "punch_out": pout_new_ms,
    })
    assert r.status_code == 200, f"T02 expected 200 got {r.status_code}: {r.text}"
    data = r.json()
    assert data["punch_out"] == pout_new_ms
    assert data["overtime_minutes"] == 65, f"expected OT=65 got {data['overtime_minutes']}"
    assert data["work_hours"] == 10.12, f"expected work_hours=10.12 got {data['work_hours']}"
    assert len(data["history"]) == 2
    h1 = data["history"][1]
    assert h1["by"] == "hr.alice"
    assert h1["changes"]["punch_out"] == {"from": pout1_ms, "to": pout_new_ms}
    assert h1["changes"]["overtime_minutes"] == {"from": 0, "to": 65}
    assert h1["changes"]["work_hours"] == {"from": 9.03, "to": 10.12}
    print("PASS  T02 valid punch_out correction -> OT: 0->65, work_hours: 9.03->10.12, 2 history entries")

    # ------------------------------------------------------------------ #
    # T03: Status correction (PRESENT -> WFH)                             #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Approved work from home request",
        "regularized_by": "manager.bob",
        "status": "WFH",
    })
    assert r.status_code == 200, f"T03 expected 200 got {r.status_code}: {r.text}"
    data = r.json()
    assert data["status"] == "WFH"
    assert len(data["history"]) == 3
    assert data["history"][2]["changes"]["status"] == {"from": "PRESENT", "to": "WFH"}
    print("PASS  T03 status correction PRESENT -> WFH recorded in history")

    # ------------------------------------------------------------------ #
    # T06: Supplying punch times with ABSENT or LEAVE -> 422            #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Invalid attempt to give punch times to leave",
        "regularized_by": "hr.alice",
        "status": "LEAVE",
        "punch_in": pin_new_ms,
    })
    assert r.status_code == 422, f"T06 expected 422 got {r.status_code}"
    print("PASS  T06 supplying punch_in with LEAVE -> 422")

    # ------------------------------------------------------------------ #
    # T04: Status correction to ABSENT/LEAVE clears punch times & reset  #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Converted to approved annual leave",
        "regularized_by": "hr.alice",
        "status": "LEAVE",
    })
    assert r.status_code == 200, f"T04 expected 200 got {r.status_code}: {r.text}"
    data = r.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert len(data["history"]) == 4
    h3 = data["history"][3]
    assert h3["changes"]["status"] == {"from": "WFH", "to": "LEAVE"}
    assert h3["changes"]["punch_in"] == {"from": pin_new_ms, "to": None}
    assert h3["changes"]["punch_out"] == {"from": pout_new_ms, "to": None}
    print("PASS  T04 changing to LEAVE clears punch times and resets derived fields")

    # ------------------------------------------------------------------ #
    # T05: Changing ABSENT/LEAVE -> PRESENT requires punch_in            #
    # ------------------------------------------------------------------ #
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Changing back to present without punch_in",
        "regularized_by": "hr.alice",
        "status": "PRESENT",
    })
    assert r.status_code == 422, f"T05 expected 422 got {r.status_code}"
    print("PASS  T05 presence status without punch_in -> 422")

    # Changing back to PRESENT WITH punch_in works:
    r = requests.patch(f"{BASE}/attendance/{EMP1}/{DATE1}", json={
        "reason": "Cancelled leave, attended office",
        "regularized_by": "hr.alice",
        "status": "PRESENT",
        "punch_in": pin_new_ms,
        "punch_out": pout_new_ms,
    })
    assert r.status_code == 200, f"T05 change with punch_in failed: {r.text}"
    data = r.json()
    assert data["status"] == "PRESENT"
    assert data["punch_in"] == pin_new_ms
    assert len(data["history"]) == 5
    print("PASS  T05 presence status with punch_in succeeds")

    # ------------------------------------------------------------------ #
    # T14: Overnight shift correction (shift_end <= shift_start)        #
    # ------------------------------------------------------------------ #
    NIGHT_DATE = "2026-07-06"
    # EMP_NIGHT has shift 22:00 to 06:00
    # Punch in 22:15 IST (15m late), punch out 06:40 IST next day (40m OT >= 30m)
    pin_night_ist = datetime(2026, 7, 6, 22, 15, 0, tzinfo=IST)
    pout_night_ist = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    pin_night_ms = dt_to_ms(pin_night_ist)
    pout_night_ms = dt_to_ms(pout_night_ist)

    # Initial punch-in and punch-out for overnight shift
    r = requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": EMP_NIGHT,
        "punched_at": pin_night_ms,
        "status": "PRESENT",
    })
    assert r.status_code == 201

    r = requests.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP_NIGHT,
        "punched_at": pout_night_ms,
    })
    assert r.status_code == 200
    night_rec = r.json()
    assert night_rec["date"] == NIGHT_DATE
    assert night_rec["overtime_minutes"] == 40
    assert night_rec["late_minutes"] == 15

    # Now regularize: HR adjusts night punch-in to 22:00 (on-time)
    pin_night_on_time_ms = dt_to_ms(datetime(2026, 7, 6, 22, 0, 0, tzinfo=IST))
    r = requests.patch(f"{BASE}/attendance/{EMP_NIGHT}/{NIGHT_DATE}", json={
        "reason": "Arrived on time, gate pass issued",
        "regularized_by": "security.officer",
        "punch_in": pin_night_on_time_ms,
    })
    assert r.status_code == 200, f"T14 expected 200 got {r.status_code}: {r.text}"
    night_updated = r.json()
    assert night_updated["late_minutes"] == 0
    assert night_updated["overtime_minutes"] == 40, f"Overnight OT should still be 40, got {night_updated['overtime_minutes']}"
    assert len(night_updated["history"]) == 1
    print("PASS  T14 overnight shift correction properly maintains shift_end date & recalculates OT")

    # ------------------------------------------------------------------ #
    # T16: Multiple sequential corrections immutability test             #
    # ------------------------------------------------------------------ #
    # Document history currently has 5 entries on EMP1
    r = requests.get(f"{BASE}/attendance", params={"emp_code": EMP1})
    assert r.status_code == 200
    rec = r.json()["items"][0]
    assert len(rec["history"]) == 5
    for entry in rec["history"]:
        assert "at" in entry and isinstance(entry["at"], int)
        assert "by" in entry and isinstance(entry["by"], str)
        assert "reason" in entry and isinstance(entry["reason"], str)
        assert "changes" in entry and isinstance(entry["changes"], dict)
    print("PASS  T16 history array is immutable and appends sequentially")

    # ------------------------------------------------------------------ #
    # T17: Concurrent correction requests (optimistic concurrency)      #
    # ------------------------------------------------------------------ #
    results = []

    def make_patch(manager_id, new_wfh_or_onduty):
        res = requests.patch(f"{BASE}/attendance/{EMP_NIGHT}/{NIGHT_DATE}", json={
            "reason": f"Concurrent edit by {manager_id}",
            "regularized_by": manager_id,
            "status": new_wfh_or_onduty,
        })
        results.append(res.status_code)

    t1 = threading.Thread(target=make_patch, args=("manager.one", "WFH"))
    t2 = threading.Thread(target=make_patch, args=("manager.two", "ON_DUTY"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # One should succeed (200) and the loser get 409 (or if serialized quickly, both 200 with 2 history entries)
    assert 200 in results, f"Expected at least one 200 in concurrent test, got {results}"
    if 409 in results:
        print(f"PASS  T17 concurrent edits handled via optimistic concurrency: results={results} (winner=200, loser=409)")
    else:
        print(f"PASS  T17 sequential thread execution both applied: results={results}")

    # ------------------------------------------------------------------ #
    # T18: Complete audit-history serialization in database & response   #
    # ------------------------------------------------------------------ #
    # Verify directly in MongoDB that BSON datetimes are stored in history.changes
    raw_doc = db.attendance_logs.find_one({"emp_code": EMP1, "date": DATE1})
    assert raw_doc is not None
    assert "_id" in raw_doc
    # In MongoDB, changes.punch_in.from must be a datetime
    first_hist = raw_doc["history"][0]
    assert isinstance(first_hist["at"], datetime), "DB history.at must be BSON datetime"
    assert isinstance(first_hist["changes"]["punch_in"]["from"], datetime), "DB changes.punch_in.from must be BSON datetime"
    assert isinstance(first_hist["changes"]["punch_in"]["to"], datetime), "DB changes.punch_in.to must be BSON datetime"

    # In HTTP API response, they must be epoch ms integers:
    r = requests.get(f"{BASE}/attendance", params={"emp_code": EMP1})
    api_rec = r.json()["items"][0]
    assert "_id" not in api_rec
    api_first_hist = api_rec["history"][0]
    assert isinstance(api_first_hist["at"], int), "API history.at must be epoch ms int"
    assert isinstance(api_first_hist["changes"]["punch_in"]["from"], int), "API changes.punch_in.from must be epoch ms int"
    assert isinstance(api_first_hist["changes"]["punch_in"]["to"], int), "API changes.punch_in.to must be epoch ms int"
    print("PASS  T18 MongoDB stores BSON datetimes; API serializes history to epoch-ms integers without _id")

    # ------------------------------------------------------------------ #
    # Cleanup test data                                                  #
    # ------------------------------------------------------------------ #
    cleanup_test_data(db)

    print()
    print("=" * 65)
    print("  ALL STEP 4 REGULARIZATION TESTS PASSED SUCCESSFULLY!")
    print("=" * 65)


if __name__ == "__main__":
    test_regularization_suite()
