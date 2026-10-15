"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import os
import re
from datetime import date as _date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

# Epoch-millisecond bounds defined by the OpenAPI contract (EpochMillis schema).
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HR attendance tracking API with analytics, leaderboard, and trend reporting.",
)

# Mount the dashboard UI at /ui
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/ui", StaticFiles(directory=_static_dir, html=True), name="ui")


@app.get("/", include_in_schema=False)
def root_redirect():
    """Redirect root to the dashboard UI."""
    return RedirectResponse(url="/ui/index.html")


# --------------------------------------------------------------------------- #
# Startup – create indexes idempotently (index_options include unique where    #
# required so concurrent duplicate creates get exactly one 201 + rest 409).   #
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes() -> None:
    # employees: unique lookup by business key
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # employees: department filter + emp_code sort used by GET /employees
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="dept_empcode",
    )
    # attendance_logs: unique natural key – also serves concurrent punch-in race
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="empcode_date_unique",
    )
    # attendance_logs: list queries sorted by date DESC, emp_code ASC
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="date_desc_empcode_asc",
    )
    # attendance_logs: analytics lookups scoped to an employee
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        name="empcode_date_asc",
    )
    # employees: joined_on filter used by analytics department summary and headcount
    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="joined_on_asc",
    )
    # employees: department + joined_on filter used by department summary with department filter
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="dept_joined_on",
    )
    # attendance_logs: late leaderboard optimization
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)],
        name="date_late_minutes",
    )


# --------------------------------------------------------------------------- #
# Timestamp helpers                                                             #
# --------------------------------------------------------------------------- #

def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds → UTC-aware datetime, truncated to whole seconds (R1)."""
    if not isinstance(ms, int) or not (EPOCH_MS_MIN <= ms <= EPOCH_MS_MAX):
        raise HTTPException(422, f"punched_at must be an integer epoch-ms in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]")
    # Truncate to whole seconds before any calculation (R1).
    s = ms // 1000
    return datetime.fromtimestamp(s, tz=timezone.utc)


def dt_to_epoch_ms(dt: datetime) -> Optional[int]:
    """Convert a datetime (aware or naive-UTC stored by PyMongo) → epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # PyMongo returns naive UTC datetimes by default; attach UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def now_utc() -> datetime:
    """Current time as UTC-aware datetime, truncated to whole seconds."""
    ts = datetime.now(tz=timezone.utc)
    return ts.replace(microsecond=0)


def ist_date_str(dt: datetime) -> str:
    """Return the IST calendar date string 'YYYY-MM-DD' for a UTC-aware datetime."""
    return dt.astimezone(IST).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Rounding helper (R4, R8)                                                     #
# --------------------------------------------------------------------------- #

def half_up(value: float, decimals: int = 2) -> float:
    """Round a float half-up (not banker's rounding) to `decimals` decimal places."""
    quantum = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business-rule helpers (R1–R5)                                                #
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """
    R2 – Grace period is 10 minutes.  Late only if punch-in is *strictly more
    than* 10:00 after shift_start.  Returns floor minutes since shift_start (not
    since the end of grace).  R1 truncation (whole seconds) is assumed already
    applied to punch_in_utc.
    """
    h, m = map(int, shift_start.split(":"))
    # Work in IST for shift arithmetic.
    punch_in_ist = punch_in_utc.astimezone(IST)
    shift_start_ist = punch_in_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    delta_seconds = int((punch_in_ist - shift_start_ist).total_seconds())
    if delta_seconds > 10 * 60:  # strictly more than 10 minutes
        return delta_seconds // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4 – (punch_out - punch_in) in seconds / 3600, half-up to 2 decimals."""
    raw = (punch_out_utc - punch_in_utc).total_seconds() / 3600
    return half_up(raw, 2)


def compute_overtime(punch_out_utc: datetime, shift_end: str, attendance_date: str) -> int:
    """
    R3 – Overtime = floor minutes between shift_end and punch_out, only when ≥ 30;
    otherwise 0.  For overnight shifts the shift_end falls on the *next* calendar day,
    but this function receives the attendance_date (the day the shift started), so
    shift_end is placed on attendance_date+1 when shift_end < shift_start.  Callers
    that need overnight handling pass the correct date_str.
    """
    h, m = map(int, shift_end.split(":"))
    # Build shift_end as an IST-aware datetime on attendance_date.
    base = datetime.fromisoformat(attendance_date).replace(
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST
    )
    raw_minutes = int((punch_out_utc.astimezone(IST) - base).total_seconds()) // 60
    return raw_minutes if raw_minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    """R5 – half_day is true when the *rounded* work_hours < 4.50."""
    return work_hours < 4.50


def derive_attendance_date(ts_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 – Derive attendance date in IST.
    For overnight shifts (shift_end <= shift_start), if the punch-in IST time
    is strictly before shift_end, it belongs to the previous day's shift.
    """
    ts_ist = ts_utc.astimezone(IST)
    sh_e, sm_e = map(int, shift_end.split(":"))
    sh_s, sm_s = map(int, shift_start.split(":"))
    is_overnight = (sh_e * 60 + sm_e) <= (sh_s * 60 + sm_s)
    if is_overnight:
        punch_ist_minutes = ts_ist.hour * 60 + ts_ist.minute
        shift_end_minutes = sh_e * 60 + sm_e
        if punch_ist_minutes < shift_end_minutes:
            ts_ist -= timedelta(days=1)
    return ts_ist.strftime("%Y-%m-%d")


def compute_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7 – Working days are Monday - Friday, no holiday calendar;
    counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    month_start = _date(year, month, 1)
    month_end = _date(year, month, num_days)

    joined_on = _date.fromisoformat(joined_on_str)
    start_date = max(month_start, joined_on)

    if start_date > month_end:
        return 0

    working_days = 0
    curr = start_date
    one_day = timedelta(days=1)
    while curr <= month_end:
        if curr.weekday() < 5:  # 0=Monday, 4=Friday
            working_days += 1
        curr += one_day

    return working_days


# --------------------------------------------------------------------------- #
# Serialisation helpers                                                        #
# --------------------------------------------------------------------------- #

def _serialize_history(history: list) -> list:
    """Convert history entries: BSON datetimes → epoch ms."""
    out = []
    for entry in history:
        changes = {}
        for field, diff in (entry.get("changes") or {}).items():
            from_val = diff.get("from")
            to_val = diff.get("to")
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch_ms(to_val)
            changes[field] = {"from": from_val, "to": to_val}
        out.append(
            {
                "at": dt_to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return out


def serialize_attendance(doc: dict) -> dict:
    """
    Convert a raw MongoDB attendance_logs document to the API AttendanceRecord shape.
    – BSON datetimes → epoch ms
    – _id removed
    – missing optional fields defaulted per DATA_MODEL.md (section 9 / legacy records)
    """
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = dt_to_epoch_ms(doc.get("punch_out"))
    doc["history"] = _serialize_history(doc.get("history") or [])
    # Provide defaults for legacy records that omit these fields (DATA_MODEL §9).
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    return doc


def serialize_employee(doc: dict) -> dict:
    """Convert a raw MongoDB employees document to the API Employee shape."""
    doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch_ms(doc.get("created_at"))
    return doc


# --------------------------------------------------------------------------- #
# Pydantic models                                                              #
# --------------------------------------------------------------------------- #

_EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
_SHIFT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
_ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not _EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        if not _SHIFT_TIME_RE.match(v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("invalid email format")
        return v

    @model_validator(mode="after")
    def validate_shift_not_equal(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch-ms")
            if not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
                raise ValueError(
                    f"punched_at must be in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]"
                )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in _PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(_PRESENCE_STATUSES)}")
        return v


# --------------------------------------------------------------------------- #
# Endpoints provided                                                           #
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    """
    Returns 200 only if MongoDB answers a ping, otherwise 503.
    FIX: was always returning 200 regardless of DB state.
    """
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    """
    FIX 1: Check-then-insert replaced by catch DuplicateKeyError (concurrency-safe).
    FIX 2: created_at stored as UTC-aware datetime; returned as epoch ms.
    FIX 3: emp_code/email/shift validation now enforced via Pydantic model.
    """
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    FIX 1: skip was `page * page_size`; corrected to `(page - 1) * page_size`.
    FIX 2: total now counts the *filtered* set (was always counting all employees).
    FIX 3: results now sorted by emp_code ascending (was unsorted).
    FIX 4: created_at serialised as epoch ms.
    FIX 5: page_size capped at 100 per R10.
    """
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size          # was: page * page_size  ← off-by-one
    total = db.employees.count_documents(q)  # was: count_documents({}) ← ignored filter
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)         # was: unsorted
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    """
    FIX 1: 404 returned when employee does not exist (was crashing with TypeError).
    FIX 2: punched_at converted via epoch_ms_to_dt (UTC-aware, whole-second truncated).
    FIX 3: Attendance date derived in IST (R1) instead of local system time.
    FIX 4: Overnight-shift date correction applied (R1).
    FIX 5: Check-then-insert replaced by DuplicateKeyError (concurrency-safe, Bug 14).
    FIX 6: punch_in stored as UTC datetime; returned as epoch ms.
    FIX 7: compute_late_minutes now IST-aware and microsecond-truncated (R1/R2).
    FIX 8: status validated to presence-only values via Pydantic model.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_dt(body.punched_at)   # UTC-aware, whole-second truncated
    else:
        ts_utc = now_utc()                          # UTC-aware, whole-second truncated

    # R1: derive attendance date in IST (handles overnight shifts)
    shift_end = emp["shift_end"]
    shift_start = emp["shift_start"]
    attendance_date = derive_attendance_date(ts_utc, shift_start, shift_end)

    late_minutes = compute_late_minutes(ts_utc, shift_start)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts_utc,       # stored as BSON datetime (UTC)
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)          # DuplicateKeyError → 409
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    FIX 1: Validate date_from <= date_to (422 if violated).
    FIX 2: Status validated against the full enum.
    FIX 3: Sorting pushed into MongoDB (.sort) instead of in-memory Python sort.
    FIX 4: Pagination (skip/limit) pushed into MongoDB – no full collection load.
    FIX 5: total uses count_documents (filtered) not len(all docs).
    FIX 6: BSON datetimes serialised to epoch ms before returning.
    FIX 7: page_size capped at 100 per R10.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    if status and status not in _ALL_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(_ALL_STATUSES)}")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)   # was: len(all docs in memory)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])  # was: in-memory sort (date only)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# POST /attendance/punch-out                                                   #
# --------------------------------------------------------------------------- #

class PunchOutIn(BaseModel):
    """Request body for POST /attendance/punch-out."""
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("punched_at must be an integer epoch-ms")
            if not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
                raise ValueError(
                    f"punched_at must be in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]"
                )
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(_ALL_STATUSES)}")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_ms(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError("timestamp must be an integer epoch-ms")
            if not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
                raise ValueError(f"timestamp must be in [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]")
        return v


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    """
    POST /attendance/punch-out

    Closes the employee's most recent attendance record whose punch_in is
    at or before punched_at and whose punch_out is still null.

    Business rules applied:
      R3  – overtime_minutes (>= 30 min after shift_end, else 0)
      R4  – work_hours (punch_out - punch_in) / 3600, half-up 2 dec
      R5  – half_day = (work_hours < 4.50)

    Concurrency:
      The MongoDB update filter includes `punch_out: null` so that two
      simultaneous requests can only both match the record on the first
      read, but only ONE find_one_and_update will see punch_out=null and
      succeed. The loser receives None and gets a 409.

    Overnight shifts (R1):
      shift_end falls on the calendar day *after* attendance_date when
      shift_end <= shift_start. The shift_end datetime is shifted forward
      by one day so that overtime is measured from the correct instant.

    History:
      Punch-out is not a manual correction; history is never written here.
    """
    # ------------------------------------------------------------------ #
    # 1. Validate employee exists                                          #
    # ------------------------------------------------------------------ #
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # ------------------------------------------------------------------ #
    # 2. Resolve punch-out timestamp (truncated to whole seconds, R1)     #
    # ------------------------------------------------------------------ #
    if body.punched_at is not None:
        pout_utc = epoch_ms_to_dt(body.punched_at)
    else:
        pout_utc = now_utc()

    # ------------------------------------------------------------------ #
    # 3. Find the most recent attendance record with a punch-in           #
    #    (works across midnight for overnight shifts)                      #
    # ------------------------------------------------------------------ #
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(404, "no punch-in found for this employee")

    pin_utc: datetime = record["punch_in"]
    # PyMongo returns naive UTC datetimes; attach UTC so arithmetic is tz-aware.
    if pin_utc.tzinfo is None:
        pin_utc = pin_utc.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------ #
    # 4. Timestamp & State validation                                      #
    #    - 422 if punched_at <= punch_in                                  #
    #    - 409 if record is already punched out                           #
    #    - 422 if punched_at > punch_in + 24 hours                        #
    # ------------------------------------------------------------------ #
    if pout_utc <= pin_utc:
        raise HTTPException(422, "punched_at must be strictly after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "that record is already punched out")

    if (pout_utc - pin_utc) > timedelta(hours=24):
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    # ------------------------------------------------------------------ #
    # 5. Compute derived fields (R3, R4, R5)                              #
    # ------------------------------------------------------------------ #
    wh = compute_work_hours(pin_utc, pout_utc)                           # R4

    shift_end = emp["shift_end"]
    shift_start = emp["shift_start"]
    attendance_date = record["date"]   # YYYY-MM-DD string (from stored record)

    # R1 overnight correction: shift_end falls on the NEXT calendar day
    # when shift_end <= shift_start (e.g. 22:00–06:00).
    sh_e, sm_e = map(int, shift_end.split(":"))
    sh_s, sm_s = map(int, shift_start.split(":"))
    is_overnight = (sh_e * 60 + sm_e) <= (sh_s * 60 + sm_s)

    if is_overnight:
        # shift_end is on attendance_date + 1 day
        base_date = _date.fromisoformat(attendance_date) + timedelta(days=1)
        ot_date_str = base_date.isoformat()
    else:
        ot_date_str = attendance_date

    ot = compute_overtime(pout_utc, shift_end, ot_date_str)             # R3
    hd = compute_half_day(wh)                                            # R5

    # ------------------------------------------------------------------ #
    # 6. Atomic update: filter includes punch_out=null so only one of     #
    #    two concurrent requests can win (the other gets None → 409).     #
    # ------------------------------------------------------------------ #
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "punch_in": pin_utc,      # must be the exact record we matched
            "punch_out": None,        # ← atomicity guard
        },
        {
            "$set": {
                "punch_out": pout_utc,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=True,         # return the post-update document
    )

    if updated is None:
        # Another concurrent request already wrote punch_out.
        raise HTTPException(409, "record already punched out")

    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    PATCH /attendance/{emp_code}/{date}

    Regularize (manually correct) one day's record.
    Omitted fields are unchanged.
    Recomputes derived fields: late_minutes, work_hours, overtime_minutes, half_day.
    Appends exactly one immutable entry to `history`.
    """
    # 1. Validate date format YYYY-MM-DD
    if not _DATE_RE.match(date):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    try:
        _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "invalid calendar date")

    # 2. Employee validation (404)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # 3. Existing attendance record validation (404)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # 4. Target status & presence rules
    target_status = body.status if body.status is not None else record.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        # Setting status ABSENT or LEAVE clears both punch times.
        # Supplying punch_in or punch_out with it is 422.
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot supply punch_in or punch_out with ABSENT or LEAVE status")
        new_pin_utc = None
        new_pout_utc = None
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Target status is a presence status (PRESENT, WFH, ON_DUTY)
        # Determine new punch_in
        if body.punch_in is not None:
            new_pin_utc = epoch_ms_to_dt(body.punch_in)
        else:
            new_pin_utc = record.get("punch_in")
            if new_pin_utc is not None and new_pin_utc.tzinfo is None:
                new_pin_utc = new_pin_utc.replace(tzinfo=timezone.utc)

        # A presence status requires a punch_in (supplied or already stored)
        if new_pin_utc is None:
            raise HTTPException(422, "a presence status requires a punch_in")

        # R1: punch_in must stay on the record's attendance date
        derived_date = derive_attendance_date(new_pin_utc, shift_start, shift_end)
        if derived_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")

        new_late_minutes = compute_late_minutes(new_pin_utc, shift_start)

        # Determine new punch_out
        if body.punch_out is not None:
            new_pout_utc = epoch_ms_to_dt(body.punch_out)
        else:
            new_pout_utc = record.get("punch_out")
            if new_pout_utc is not None and new_pout_utc.tzinfo is None:
                new_pout_utc = new_pout_utc.replace(tzinfo=timezone.utc)

        if new_pout_utc is not None:
            if new_pout_utc <= new_pin_utc:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if (new_pout_utc - new_pin_utc) > timedelta(hours=24):
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

            new_work_hours = compute_work_hours(new_pin_utc, new_pout_utc)

            # Overnight shift handling for overtime calculation
            sh_e, sm_e = map(int, shift_end.split(":"))
            sh_s, sm_s = map(int, shift_start.split(":"))
            is_overnight = (sh_e * 60 + sm_e) <= (sh_s * 60 + sm_s)
            if is_overnight:
                ot_date_str = (_date.fromisoformat(date) + timedelta(days=1)).isoformat()
            else:
                ot_date_str = date

            new_overtime_minutes = compute_overtime(new_pout_utc, shift_end, ot_date_str)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # 5. Detect changes
    old_pin = record.get("punch_in")
    if old_pin is not None and old_pin.tzinfo is None:
        old_pin = old_pin.replace(tzinfo=timezone.utc)

    old_pout = record.get("punch_out")
    if old_pout is not None and old_pout.tzinfo is None:
        old_pout = old_pout.replace(tzinfo=timezone.utc)

    changes = {}

    if record.get("status") != target_status:
        changes["status"] = {"from": record.get("status"), "to": target_status}

    if old_pin != new_pin_utc:
        changes["punch_in"] = {"from": old_pin, "to": new_pin_utc}

    if old_pout != new_pout_utc:
        changes["punch_out"] = {"from": old_pout, "to": new_pout_utc}

    if record.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}

    if record.get("late_minutes", 0) != new_late_minutes:
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}

    if record.get("overtime_minutes", 0) != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}

    if record.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    # 6. Reject no-op corrections (422)
    if not changes:
        raise HTTPException(422, "a request that changes nothing is rejected")

    # 7. Build history entry
    correction_time = now_utc()
    history_entry = {
        "at": correction_time,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # 8. Optimistic concurrency control using history array size
    existing_history = record.get("history") or []
    history_len = len(existing_history)

    filter_query = {
        "_id": record["_id"],
        "emp_code": emp_code,
        "date": date,
    }
    if history_len == 0:
        filter_query["$or"] = [
            {"history": {"$exists": False}},
            {"history": {"$size": 0}},
        ]
    else:
        filter_query["history"] = {"$size": history_len}

    update_ops = {
        "$set": {
            "status": target_status,
            "punch_in": new_pin_utc,
            "punch_out": new_pout_utc,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        update_ops,
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "conflict: record was modified concurrently")

    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints                                                         #
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    GET /analytics/employees/{emp_code}/monthly

    Monthly attendance summary for one employee.
    Backed by a MongoDB aggregation pipeline on attendance_logs.
    - working_days: Mon-Fri per R7, counted from joined_on.
    - present_days: PRESENT, WFH, ON_DUTY on Mon-Fri per R5-R7 (half_day = 0.5).
    - leave_days: count of LEAVE records.
    - late_count: records with late_minutes > 0 (includes weekends per R7).
    - total_late_minutes: sum of late_minutes (includes weekends per R7).
    - total_overtime_minutes: sum of overtime_minutes (includes weekends per R7).
    - attendance_pct: present_days / working_days * 100, R8 rounding (2 decimals),
      or null when working_days == 0.
    404 if employee does not exist.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = compute_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        agg = results[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    GET /analytics/departments/summary

    Department summary for a month via MongoDB aggregation across both collections.
    - headcount: employees whose joined_on <= month end (R9), including those with 0 logs.
    - present_days: PRESENT, WFH, ON_DUTY on Mon-Fri per R5-R7 (half_day = 0.5).
    - avg_work_hours: mean work_hours over records that are present-status AND have work_hours;
      null if none. R8 half-up rounding to 2 decimals.
    - late_count: count of records with late_minutes > 0 (includes weekends per R7).
    - total_late_minutes: sum of late_minutes (includes weekends per R7).
    - leave_count: count of LEAVE records.
    - on_duty_count: count of ON_DUTY records.
    A department whose headcount is 0 for the month is omitted.
    Results sorted by department ascending.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_stage: dict = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]},
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$pl.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "wl",
                            "in": "$$wl.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$eq": ["$$l.status", "LEAVE"]
                            },
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$eq": ["$$l.status", "ON_DUTY"]
                            },
                        }
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        avg_wh = doc.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = half_up(avg_wh, 2)
        items.append(
            {
                "department": doc["department"],
                "headcount": int(doc["headcount"]),
                "present_days": float(doc["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": int(doc["late_count"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "leave_count": int(doc["leave_count"]),
                "on_duty_count": int(doc["on_duty_count"]),
            }
        )

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    GET /analytics/leaderboard/late

    Ranks employees by total late_minutes in the month (only employees with total > 0), highest first.
    - Standard competition ranking: ties share a rank, next rank is skipped (1, 2, 2, 4).
    - limit applies after ranking: returns every employee whose rank <= limit.
    - When department is given, ranking happens inside that department only.
    - Order: total_late_minutes descending, then emp_code ascending.
    - name and department come from employees collection; logs whose employee does not exist are ignored.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": str(doc["emp_code"]),
            "name": str(doc["name"]),
            "department": str(doc["department"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    GET /analytics/departments/{department}/trend

    Daily attendance trend for a department.
    Returns exactly one row per calendar day from `from` to `to` inclusive (using $densify).
    - is_working_day = Mon-Fri.
    - headcount = department employees with joined_on <= that day (R9).
    - present_count = presence-status records that day (half day = 0.5), any weekday or weekend.
    - late_count = records that day with late_minutes > 0.
    - attendance_rate = present_count / headcount, 4 decimals half-up, only on working days with headcount > 0, otherwise null.
      A working day with no records has rate 0.0.
    - moving_avg_7d = mean of non-null attendance_rate values among the current row and up to 6 preceding rows inside the requested range.
      null if the window holds no non-null rate. 4 decimals half-up.
    Errors:
    - 404 unknown department (no employees in department).
    - 422 if to < from or the range exceeds 92 days.
    """
    try:
        from_date = _date.fromisoformat(from_)
        to_date = _date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")

    if to_date < from_date:
        raise HTTPException(422, "to date must be >= from date")

    if (to_date - from_date).days > 92:
        raise HTTPException(422, "date range exceeds 92 days")

    # 404 unknown department (no employees)
    dept_exists = db.employees.find_one({"department": department}, {"_id": 1})
    if not dept_exists:
        raise HTTPException(404, "department not found")

    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc)
    to_dt_plus_1 = to_dt + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_, "$lte": to}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        att_rate = doc.get("attendance_rate")
        if att_rate is not None:
            att_rate = half_up(att_rate, 4)

        mov_avg = doc.get("moving_avg_7d")
        if mov_avg is not None:
            mov_avg = half_up(mov_avg, 4)

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(doc["present_count"]),
            "late_count": int(doc["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": mov_avg,
        })

    return {
        "department": department,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Step 9: Explain Endpoint                                                    #
#   GET   /admin/explain/{endpoint}                                           #
# --------------------------------------------------------------------------- #

class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainEndpointEnum,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    GET /admin/explain/{endpoint}

    Runs MongoDB explain with executionStats verbosity for the main query or aggregation
    of the named endpoint, built exactly as that endpoint builds it (same filters, sort,
    pagination), and returns the raw explain output as JSON.
    """
    if endpoint == ExplainEndpointEnum.attendance_list:
        if date_from and date_to:
            try:
                d_from = _date.fromisoformat(date_from)
                d_to = _date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")
            if d_from > d_to:
                raise HTTPException(422, "date_from must be <= date_to")
        if status and status not in _ALL_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(_ALL_STATUSES)}")

        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        explain_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        collection_name = "attendance_logs"

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    elif endpoint == ExplainEndpointEnum.department_summary:
        if not month:
            raise HTTPException(422, "month is required for department_summary")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        match_stage: dict = {"joined_on": {"$lte": end_date}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", start_date]},
                                        {"$lte": ["$date", end_date]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "all_logs": {"$push": "$logs"},
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": 1,
                    "logs": {
                        "$reduce": {
                            "input": "$all_logs",
                            "initialValue": [],
                            "in": {"$concatArrays": ["$$value", "$$this"]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": 1,
                    "headcount": 1,
                    "present_days": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$logs",
                                        "as": "l",
                                        "cond": {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]},
                                            ]
                                        },
                                    }
                                },
                                "as": "p",
                                "in": {
                                    "$cond": [
                                        {"$eq": ["$$p.half_day", True]},
                                        0.5,
                                        1.0,
                                    ]
                                },
                            }
                        }
                    },
                    "work_hours_list": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        "$filter": {
                                            "input": "$logs",
                                            "as": "l",
                                            "cond": {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        }
                                    },
                                    "as": "p",
                                    "in": "$$p.work_hours",
                                }
                            },
                            "as": "wh",
                            "cond": {"$ne": ["$$wh", None]},
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                            }
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {"$ifNull": ["$$l.late_minutes", 0]},
                            }
                        }
                    },
                    "leave_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "LEAVE"]},
                            }
                        }
                    },
                    "on_duty_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                            }
                        }
                    },
                }
            },
            {"$sort": {"department": 1}},
        ]
        explain_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "employees"

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    elif endpoint == ExplainEndpointEnum.department_trend:
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_:
            raise HTTPException(422, "from is required for department_trend")
        if not to:
            raise HTTPException(422, "to is required for department_trend")

        try:
            from_date = _date.fromisoformat(from_)
            to_date = _date.fromisoformat(to)
        except ValueError:
            raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")

        if to_date < from_date:
            raise HTTPException(422, "to date must be >= from date")

        if (to_date - from_date).days > 92:
            raise HTTPException(422, "date range exceeds 92 days")

        from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
        to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc)
        to_dt_plus_1 = to_dt + timedelta(days=1)

        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt_plus_1],
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                    "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day_str": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$day_str"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "hc",
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
        ]
        explain_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        collection_name = "attendance_logs"

    else:
        raise HTTPException(422, f"Unknown endpoint '{endpoint}'")

    try:
        raw_explain = db.command("explain", explain_cmd, verbosity="executionStats")
        clean_explain = json.loads(json_util.dumps(raw_explain))
    except Exception as e:
        raise HTTPException(500, f"Error running explain: {str(e)}")

    return {
        "endpoint": endpoint.value if isinstance(endpoint, Enum) else str(endpoint),
        "collection": collection_name,
        "explain": clean_explain,
    }




