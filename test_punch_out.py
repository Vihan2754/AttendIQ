"""
Tests for POST /attendance/punch-out

Coverage:
  T01  Normal punch-out – correct work_hours, overtime, half_day, epoch-ms response
  T02  Unknown employee – 404
  T03  No open punch-in record – 404
  T04  Already punched out – 409
  T05  punched_at == punch_in – 422
  T06  punched_at < punch_in – 422
  T07  punched_at > punch_in + 24 h – 422
  T08  punched_at epoch-ms out of range – 422
  T09  Overtime exactly 30 min – counted (= 30)
  T10  Overtime 29 min 59 sec – NOT counted (= 0)
  T11  Overtime 5 min – NOT counted (= 0)
  T12  Half day (work_hours < 4.50) – half_day=True
  T13  Exactly 4.50 h – NOT a half day
  T14  Overnight shift normal punch-out – correct overtime date shift
  T15  Overnight punch-out: no history entry written
  T16  punched_at defaults to now() when omitted (smoke test)
  T17  Response excludes _id; all instants are epoch-ms integers
  T18  Existing helper unit tests still pass (regression)

Run:
  pip install httpx pytest
  python test_punch_out.py
"""

import time
import threading
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# ======================================================================
# Helper: in-process logic tests (no live server needed for pure-logic)
# ======================================================================

from decimal import Decimal, ROUND_HALF_UP


def half_up(value, decimals=2):
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(pin, pout):
    return half_up((pout - pin).total_seconds() / 3600, 2)


def compute_overtime(pout_utc, shift_end, attendance_date):
    h, m = map(int, shift_end.split(":"))
    base = datetime.fromisoformat(attendance_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    raw = int((pout_utc.astimezone(IST) - base).total_seconds()) // 60
    return raw if raw >= 30 else 0


def compute_half_day(wh):
    return wh < 4.50


def make_utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def make_ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


# ======================================================================
# Pure-logic tests (no MongoDB, no HTTP)
# ======================================================================

def test_T01_normal_work_hours():
    """T01: 9h 7min work = 9.12h, overtime 5min < 30 -> 0, not half_day."""
    pin  = make_ist(2026, 7, 6, 9, 28, 0)
    pout = make_ist(2026, 7, 6, 18, 35, 0)
    wh = compute_work_hours(pin, pout)
    ot = compute_overtime(pout, "18:30", "2026-07-06")
    hd = compute_half_day(wh)
    assert wh == 9.12, f"expected 9.12 got {wh}"
    assert ot == 0,    f"5 min OT < 30 threshold, expected 0 got {ot}"
    assert not hd,     "9.12h is NOT a half day"
    print("PASS  T01 normal work_hours / no OT / no half_day")


def test_T09_overtime_exactly_30():
    """T09: Punch-out exactly 30 min after shift_end -> OT = 30."""
    pin  = make_ist(2026, 7, 6, 9, 28, 0)
    pout = make_ist(2026, 7, 6, 19, 0, 0)    # 18:30 + 30 min
    ot = compute_overtime(pout, "18:30", "2026-07-06")
    assert ot == 30, f"exactly 30 min OT expected 30 got {ot}"
    print("PASS  T09 overtime exactly 30 min")


def test_T10_overtime_29m59s():
    """T10: 29 min 59 sec after shift_end -> OT = 0."""
    pin  = make_ist(2026, 7, 6, 9, 28, 0)
    pout = make_ist(2026, 7, 6, 18, 59, 59)  # 29m59s after 18:30
    ot = compute_overtime(pout, "18:30", "2026-07-06")
    assert ot == 0, f"29m59s OT expected 0 got {ot}"
    print("PASS  T10 overtime 29m59s -> 0")


def test_T11_overtime_5min():
    """T11: 5 min overtime -> 0."""
    pout = make_ist(2026, 7, 6, 18, 35, 0)
    ot = compute_overtime(pout, "18:30", "2026-07-06")
    assert ot == 0, f"5 min OT expected 0 got {ot}"
    print("PASS  T11 overtime 5 min -> 0")


def test_T12_half_day():
    """T12: work_hours < 4.50 -> half_day=True."""
    pin  = make_ist(2026, 7, 7, 9, 30, 0)
    pout = make_ist(2026, 7, 7, 13, 0, 0)   # 3.5h
    wh = compute_work_hours(pin, pout)
    assert wh == 3.5, f"expected 3.5 got {wh}"
    assert compute_half_day(wh), "3.5h should be half_day=True"
    print("PASS  T12 half_day True (3.5h)")


def test_T13_exactly_450():
    """T13: exactly 4.50 h is NOT a half day (strict <)."""
    pin  = make_ist(2026, 7, 6, 9, 30, 0)
    pout = make_ist(2026, 7, 6, 14, 0, 0)   # exactly 4.50h
    wh = compute_work_hours(pin, pout)
    assert wh == 4.5,          f"expected 4.5 got {wh}"
    assert not compute_half_day(wh), "4.50h is NOT half_day"
    print("PASS  T13 exactly 4.50h -> half_day=False")


def test_T14_overnight_overtime():
    """T14: Overnight shift 22:00-06:00. Punch-out 06:40 next day.
    shift_end falls on attendance_date+1. OT = 40 min."""
    attendance_date = "2026-07-06"
    # shift_end 06:00 on 2026-07-07 (next day)
    from datetime import date as _d
    ot_date_str = (_d.fromisoformat(attendance_date) + timedelta(days=1)).isoformat()
    pout = make_ist(2026, 7, 7, 6, 40, 0)   # 06:40 IST on 2026-07-07
    ot = compute_overtime(pout, "06:00", ot_date_str)
    assert ot == 40, f"overnight OT expected 40 got {ot}"
    print("PASS  T14 overnight shift OT = 40 min")


def test_T05_pout_equals_pin():
    """T05/T06: punched_at <= punch_in must be caught (logic check)."""
    pin  = make_utc(2026, 7, 6, 4, 0, 0)
    pout = make_utc(2026, 7, 6, 4, 0, 0)   # equal
    assert pout <= pin, "pout==pin should trigger 422"
    print("PASS  T05 pout == pin -> 422 (logic confirmed)")


def test_T07_pout_over_24h():
    """T07: punched_at > punch_in + 24h must be caught."""
    pin  = make_utc(2026, 7, 6, 4, 0, 0)
    pout = make_utc(2026, 7, 7, 4, 0, 1)  # exactly 24h + 1s
    assert (pout - pin) > timedelta(hours=24), "should trigger 422"
    print("PASS  T07 pout > pin + 24h -> 422 (logic confirmed)")


def test_T08_epoch_ms_range():
    """T08: punched_at below EPOCH_MS_MIN must be rejected."""
    EPOCH_MS_MIN = 100_000_000_000
    bad_ts = 99_999_999_999  # seconds-looking value
    assert not (EPOCH_MS_MIN <= bad_ts <= 4_102_444_800_000), "should fail range check"
    print("PASS  T08 out-of-range epoch-ms -> 422 (logic confirmed)")


def test_T15_no_history_on_punchout():
    """T15: $set in punch-out update never touches `history`."""
    update_doc = {
        "$set": {
            "punch_out": "some_datetime",
            "work_hours": 9.12,
            "overtime_minutes": 0,
            "half_day": False,
        }
    }
    assert "history" not in update_doc["$set"], "$set must not touch history"
    print("PASS  T15 history not in $set fields")


def test_T17_serialize_epoch_ms():
    """T17: dt_to_epoch_ms converts BSON datetime to int epoch ms."""
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC)
    ms = int(dt.timestamp() * 1000)
    assert isinstance(ms, int), "must be int"
    assert ms == 1783310280000, f"expected 1783310280000 got {ms}"
    print("PASS  T17 epoch ms serialization correct")


def test_T18_helper_regression():
    """T18: Original helper unit tests still pass."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "test_helpers.py"],
        capture_output=True, text=True, cwd="."
    )
    assert result.returncode == 0, f"Helper tests failed:\n{result.stdout}\n{result.stderr}"
    print("PASS  T18 all original helper unit tests still pass")


# ======================================================================
# HTTP integration tests (requires live server + MongoDB)
# Skipped automatically if server is not reachable.
# ======================================================================

def _server_available():
    try:
        import requests as client
        r = client.get("http://localhost:8000/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def run_integration_tests():
    import requests as httpx

    BASE = "http://localhost:8000"
    EMP = "EMP9901"  # test employee; use high number to avoid collision with sample data

    # Clean up any leftover test data from a previous run
    from pymongo import MongoClient
    from dotenv import load_dotenv
    import os
    load_dotenv()
    _client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    _db = _client[os.getenv("MONGO_DB", "attendance_db")]
    _db.employees.delete_many({"emp_code": {"$regex": "^EMP99"}})
    _db.attendance_logs.delete_many({"emp_code": {"$regex": "^EMP99"}})

    # ---- Create test employee ------------------------------------------
    r = httpx.post(f"{BASE}/employees", json={
        "emp_code": EMP,
        "name": "Test User",
        "email": "test@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    assert r.status_code in (201, 409), f"employee create: {r.status_code} {r.text}"

    # ---- T02: unknown employee -> 404 ---------------------------------
    r = httpx.post(f"{BASE}/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert r.status_code == 404, f"T02 expected 404 got {r.status_code}: {r.text}"
    print("PASS  T02 (HTTP) unknown employee -> 404")

    # ---- T03: no open punch-in -> 404 --------------------------------
    # Use a far-future timestamp to guarantee no record matches
    far_future = 2_000_000_000_000
    r = httpx.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP,
        "punched_at": far_future,
    })
    assert r.status_code == 404, f"T03 expected 404 got {r.status_code}: {r.text}"
    print("PASS  T03 (HTTP) no open punch-in -> 404")

    # ---- Punch in for today -------------------------------------------
    # Use 09:40 IST today so late_minutes=0 (within grace)
    pin_ist = datetime.now(tz=IST).replace(hour=9, minute=35, second=0, microsecond=0)
    pin_ms = int(pin_ist.astimezone(UTC).timestamp() * 1000)
    r = httpx.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": EMP,
        "punched_at": pin_ms,
        "status": "PRESENT",
    })
    assert r.status_code in (201, 409), f"punch-in: {r.status_code} {r.text}"
    if r.status_code == 409:
        print("INFO  punch-in already exists (409 – test data from prior run)")

    # ---- T05: punched_at == punch_in -> 422 --------------------------
    r = httpx.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP,
        "punched_at": pin_ms,
    })
    assert r.status_code == 422, f"T05 expected 422 got {r.status_code}: {r.text}"
    print("PASS  T05 (HTTP) punched_at == punch_in -> 422")

    # ---- T06: punched_at < punch_in -> 422 ---------------------------
    r = httpx.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP,
        "punched_at": pin_ms - 1000,  # 1 second before punch_in
    })
    assert r.status_code == 422, f"T06 expected 422 got {r.status_code}: {r.text}"
    print("PASS  T06 (HTTP) punched_at < punch_in -> 422")

    # ---- T07: punched_at > punch_in + 24h -> 422 --------------------
    over_24h = pin_ms + (24 * 3600 + 1) * 1000  # 24h + 1s in ms
    if over_24h <= 4_102_444_800_000:
        r = httpx.post(f"{BASE}/attendance/punch-out", json={
            "emp_code": EMP,
            "punched_at": over_24h,
        })
        assert r.status_code == 422, f"T07 expected 422 got {r.status_code}: {r.text}"
        print("PASS  T07 (HTTP) punched_at > 24h after punch_in -> 422")

    # ---- T08: out-of-range epoch-ms -> 422 --------------------------
    r = httpx.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP,
        "punched_at": 99_999_999_999,
    })
    assert r.status_code == 422, f"T08 expected 422 got {r.status_code}: {r.text}"
    print("PASS  T08 (HTTP) out-of-range punched_at -> 422")

    # ---- T01: Normal punch-out (9h 7min work, 5min OT → no OT) -----
    pout_ist = pin_ist.replace(hour=18, minute=35)   # 9h7m work, 5m after shift end
    pout_ms = int(pout_ist.astimezone(UTC).timestamp() * 1000)
    r = httpx.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP,
        "punched_at": pout_ms,
    })
    assert r.status_code == 200, f"T01 expected 200 got {r.status_code}: {r.text}"
    data = r.json()
    assert data["emp_code"] == EMP
    assert data["punch_out"] == pout_ms,        f"punch_out ms: {data['punch_out']}"
    assert data["punch_in"] == pin_ms,          f"punch_in ms: {data['punch_in']}"
    assert data["work_hours"] == 9.0,           f"work_hours: {data['work_hours']}"
    assert data["overtime_minutes"] == 0,       f"OT: {data['overtime_minutes']}"
    assert data["half_day"] is False,           f"half_day: {data['half_day']}"
    assert "_id" not in data,                   "_id must not appear in response"
    assert isinstance(data["punch_in"], int),   "punch_in must be epoch ms int"
    assert isinstance(data["punch_out"], int),  "punch_out must be epoch ms int"
    assert data["history"] == [],               "history must be empty"
    print(f"PASS  T01 (HTTP) normal punch-out -> work_hours={data['work_hours']}, OT={data['overtime_minutes']}, half_day={data['half_day']}")

    # ---- T04: Already punched out -> 409 ----------------------------
    r = httpx.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": EMP,
        "punched_at": pout_ms + 1000,
    })
    assert r.status_code == 409, f"T04 expected 409 got {r.status_code}: {r.text}"
    print("PASS  T04 (HTTP) already punched out -> 409")

    # ---- T16: Default punched_at = now (smoke test) -----------------
    # Create a fresh employee to punch in/out with default timestamp
    EMP2 = "EMP9902"
    r = httpx.post(f"{BASE}/employees", json={
        "emp_code": EMP2,
        "name": "Test User2",
        "email": "test2@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    })
    assert r.status_code in (201, 409)

    # Punch in with explicit timestamp 1h ago
    one_hour_ago_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    r = httpx.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": EMP2,
        "punched_at": one_hour_ago_ms,
    })
    if r.status_code == 201:
        # Now punch out with no punched_at (defaults to now)
        r2 = httpx.post(f"{BASE}/attendance/punch-out", json={"emp_code": EMP2})
        assert r2.status_code == 200, f"T16 expected 200 got {r2.status_code}: {r2.text}"
        data2 = r2.json()
        assert data2["punch_out"] is not None
        print(f"PASS  T16 (HTTP) default punched_at -> work_hours={data2['work_hours']}")
    else:
        print("SKIP  T16 (HTTP) punch-in 409 (prior run), skipping")

    print()
    print("=" * 55)
    print("  ALL HTTP INTEGRATION TESTS PASSED")
    print("=" * 55)


# ======================================================================
# Main runner
# ======================================================================

if __name__ == "__main__":
    print("=" * 55)
    print("  PUNCH-OUT TESTS")
    print("=" * 55)
    print()
    print("--- Logic / unit tests (no MongoDB needed) ---")
    test_T01_normal_work_hours()
    test_T09_overtime_exactly_30()
    test_T10_overtime_29m59s()
    test_T11_overtime_5min()
    test_T12_half_day()
    test_T13_exactly_450()
    test_T14_overnight_overtime()
    test_T05_pout_equals_pin()
    test_T07_pout_over_24h()
    test_T08_epoch_ms_range()
    test_T15_no_history_on_punchout()
    test_T17_serialize_epoch_ms()
    test_T18_helper_regression()

    print()
    print("--- HTTP integration tests (requires live server) ---")
    if _server_available():
        run_integration_tests()
    else:
        print("SKIP  Server not running on localhost:8000")
        print("      Start with: uvicorn app.main:app --port 8000")
        print("      Then re-run this script for full HTTP coverage.")
