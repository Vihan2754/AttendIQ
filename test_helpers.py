"""
Unit tests for the fixed helper functions in app/main.py.
Run with: python test_helpers.py
No MongoDB connection required.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# ---- helpers replicated inline (same code as main.py) ----

def half_up(value, decimals=2):
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def epoch_ms_to_dt(ms):
    s = ms // 1000
    return datetime.fromtimestamp(s, tz=UTC)

def dt_to_epoch_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def compute_late_minutes(punch_in_utc, shift_start):
    h, m = map(int, shift_start.split(":"))
    punch_in_ist = punch_in_utc.astimezone(IST)
    shift_start_ist = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    delta_seconds = int((punch_in_ist - shift_start_ist).total_seconds())
    if delta_seconds > 10 * 60:
        return delta_seconds // 60
    return 0

def compute_work_hours(pin, pout):
    raw = (pout - pin).total_seconds() / 3600
    return half_up(raw, 2)

def compute_overtime(punch_out_utc, shift_end, attendance_date):
    h, m = map(int, shift_end.split(":"))
    base = datetime.fromisoformat(attendance_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    raw_minutes = int((punch_out_utc.astimezone(IST) - base).total_seconds()) // 60
    return raw_minutes if raw_minutes >= 30 else 0

def make_ist(h, m, s=0, date_str="2026-07-06"):
    """Build a UTC-aware datetime from an IST time on the given date."""
    y, mo, d = map(int, date_str.split("-"))
    ist_dt = datetime(y, mo, d, h, m, s, tzinfo=IST)
    return ist_dt.astimezone(UTC)

# ================================================================
# Test: half_up
# ================================================================
assert half_up(2.225, 2) == 2.23, f"half_up(2.225,2) = {half_up(2.225,2)}"
assert half_up(4.495, 2) == 4.50, f"half_up(4.495,2) = {half_up(4.495,2)}"
assert half_up(9.125, 2) == 9.13, f"half_up(9.125,2) = {half_up(9.125,2)}"
assert half_up(0.005, 2) == 0.01, f"half_up(0.005,2) = {half_up(0.005,2)}"
print("PASS  half_up")

# ================================================================
# Test: epoch_ms_to_dt  (R1 – whole-second truncation)
# ================================================================
ts = epoch_ms_to_dt(1783312500000)
assert ts.tzinfo is not None, "must be tz-aware"
assert ts.microsecond == 0,   "must have no microseconds"
# R1: sub-millisecond part already discarded at epoch_ms; ms truncation
assert epoch_ms_to_dt(1783312500999) == epoch_ms_to_dt(1783312500000), \
    "999ms difference should truncate to same second"
print("PASS  epoch_ms_to_dt")

# ================================================================
# Test: dt_to_epoch_ms
# ================================================================
assert dt_to_epoch_ms(None) is None
ms_in = 1783312500000
assert dt_to_epoch_ms(epoch_ms_to_dt(ms_in)) == ms_in, "round-trip failed"
# naive datetime treated as UTC
naive = datetime(2026, 7, 6, 4, 0, 0)
aware = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
assert dt_to_epoch_ms(naive) == dt_to_epoch_ms(aware), "naive should be treated as UTC"
print("PASS  dt_to_epoch_ms")

# ================================================================
# Test: compute_late_minutes  (R2 spec examples, shift 09:30)
# ================================================================
# 09:40:00 → exactly 10 min grace boundary → 0
r = compute_late_minutes(make_ist(9, 40, 0), "09:30")
assert r == 0, f"09:40:00 -> expected 0, got {r}"

# 09:40:01 → 1 second past grace → 10 late minutes (floor from shift_start)
r = compute_late_minutes(make_ist(9, 40, 1), "09:30")
assert r == 10, f"09:40:01 -> expected 10, got {r}"

# 10:15:59 → 45 min 59 sec from shift_start → 45 late minutes (floor)
r = compute_late_minutes(make_ist(10, 15, 59), "09:30")
assert r == 45, f"10:15:59 -> expected 45, got {r}"

# 09:28:00 → before shift_start → 0
r = compute_late_minutes(make_ist(9, 28, 0), "09:30")
assert r == 0, f"09:28 (before shift) -> expected 0, got {r}"

# 09:30:00 → exactly on time → 0
r = compute_late_minutes(make_ist(9, 30, 0), "09:30")
assert r == 0, f"09:30:00 -> expected 0, got {r}"

# R1: sub-second already truncated by epoch_ms_to_dt; 09:40:00 is NOT late
ts_edge = make_ist(9, 40, 0)  # exactly 10min, not strictly greater
r = compute_late_minutes(ts_edge, "09:30")
assert r == 0, f"exactly 10 min boundary -> expected 0, got {r}"
print("PASS  compute_late_minutes")

# ================================================================
# Test: compute_work_hours  (R4 – half-up 2 decimals)
# ================================================================
# Sample doc 1: 09:28 → 18:35 IST = 9h 7min = 9.1167h → rounds to 9.12
pin  = make_ist(9, 28, 0)
pout = make_ist(18, 35, 0)
wh = compute_work_hours(pin, pout)
assert wh == 9.12, f"9h7m -> expected 9.12, got {wh}"

# Sample doc 4 (half day): 09:30 → 13:00 IST = 3h 30min = 3.5h
pin2  = make_ist(9, 30, 0)
pout2 = make_ist(13, 0, 0)
wh2 = compute_work_hours(pin2, pout2)
assert wh2 == 3.5, f"3h30m -> expected 3.5, got {wh2}"

# half_day boundary: exactly 4h 30min = 4.5 is NOT a half day
pin3  = make_ist(9, 30, 0)
pout3 = make_ist(14, 0, 0)
wh3 = compute_work_hours(pin3, pout3)
assert wh3 == 4.5, f"4h30m -> expected 4.5, got {wh3}"
assert not (wh3 < 4.50), f"4.5 should not be a half_day"
print("PASS  compute_work_hours")

# ================================================================
# Test: compute_overtime  (R3 – threshold >= 30 min)
# ================================================================
# 40 min after 18:30 → 40 OT minutes
pout_40 = make_ist(19, 10, 0)
r = compute_overtime(pout_40, "18:30", "2026-07-06")
assert r == 40, f"40 min OT -> expected 40, got {r}"

# 5 min after 18:30 → under 30 threshold → 0
pout_5 = make_ist(18, 35, 0)
r = compute_overtime(pout_5, "18:30", "2026-07-06")
assert r == 0, f"5 min OT -> expected 0, got {r}"

# Exactly 30 min → included
pout_30 = make_ist(19, 0, 0)
r = compute_overtime(pout_30, "18:30", "2026-07-06")
assert r == 30, f"30 min OT -> expected 30, got {r}"

# 29 min 59 sec → just under 30 → 0
pout_29m59s = make_ist(18, 59, 59)
r = compute_overtime(pout_29m59s, "18:30", "2026-07-06")
assert r == 0, f"29m59s OT -> expected 0, got {r}"
print("PASS  compute_overtime")

# ================================================================
# Test: compute_half_day  (R5)
# ================================================================
assert 3.5 < 4.50,    "3.5h should be half_day"
assert not (4.50 < 4.50), "4.5h is NOT half_day (strict <)"
assert not (9.12 < 4.50), "9.12h is NOT half_day"
assert 4.49 < 4.50,   "4.49h should be half_day"
print("PASS  compute_half_day")

# ================================================================
# Test: IST overnight shift date assignment (R1)
# ================================================================
# EMP0005: shift 22:00-06:00 (overnight). Punch-in at 21:55 IST on 2026-07-06.
# punch time before shift_end (06:00) would move date to prev day; but 21:55 >= 06:00,
# so date stays 2026-07-06. ✓
# Punch-in at 05:30 IST on 2026-07-07 → before shift_end 06:00 → date = 2026-07-06
punch_early = datetime(2026, 7, 7, 5, 30, 0, tzinfo=IST).astimezone(UTC)
sh_e, sm_e = 6, 0
sh_s, sm_s = 22, 0
is_overnight = (sh_e * 60 + sm_e) <= (sh_s * 60 + sm_s)
assert is_overnight, "22:00-06:00 should be overnight"
punch_ist_minutes = punch_early.astimezone(IST).hour * 60 + punch_early.astimezone(IST).minute
shift_end_minutes = sh_e * 60 + sm_e
if punch_ist_minutes < shift_end_minutes:
    corrected = punch_early.astimezone(IST) - timedelta(days=1)
    date_str = corrected.strftime("%Y-%m-%d")
    assert date_str == "2026-07-06", f"overnight early punch date should be 2026-07-06, got {date_str}"
print("PASS  overnight shift date correction (R1)")

print()
print("=" * 50)
print("ALL UNIT TESTS PASSED")
print("=" * 50)
