# AttendIQ — Employee Attendance & Analytics API & Dashboard

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.10%20|%203.11%20|%203.12-3776ab?logo=python&logoColor=white)](https://python.org)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.0+-47A248?logo=mongodb&logoColor=white)](https://www.mongodb.com)
[![Status](https://img.shields.io/badge/Tests-8%2F8%20Suites%20Passed-10b981)]()

A production-ready, full-stack HR attendance tracking service and analytics engine built with **FastAPI**, **MongoDB 6.0+**, and a modern **interactive Web UI Dashboard**. The service implements end-to-end employee lifecycle management (clock-in, clock-out, regularization audit trails) and high-performance server-side analytical aggregations following strict business rules (**R1–R10**) and OpenAPI contract specifications.

---

## 🌟 Key Highlights & Web Dashboard

In addition to the REST API endpoints, the application includes a glassmorphic Web UI Dashboard available directly in the browser:

👉 **Web Dashboard**: [http://localhost:8000/ui/index.html](http://localhost:8000/ui/index.html)  
👉 **Interactive API Documentation (Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)  
👉 **Service Health Check**: [http://localhost:8000/health](http://localhost:8000/health)

### Dashboard Capabilities
- **Overview & Real-Time KPIs**: Live metrics for total headcount, active presence rate, late minutes, and accumulated overtime hours.
- **Symmetrical Punch In & Punch Out**: Clean time-only input controls that automatically default to today's IST date (`2026-10-16`) in the background, complete with 4 one-click quick presets (`Current Time`, `Shift Start/End`, `Late`, `Half-Day`, `Overtime`) and live epoch millisecond conversion.
- **Attendance Regularization Form**: Submit manager corrections with mandatory reason audit logs, protected by optimistic concurrency locking.
- **Interactive Staff Directory**: Paginated employee table with live department filtering and a quick "Add Employee" modal.
- **Monthly Analytics & Department Summary**: Inspect individual and department-wide attendance metrics, presence rates, and working day summaries.
- **7-Day Department Trend**: Dynamic Chart.js multi-line visualization rendering daily attendance trends with MongoDB `$densify` zero-gap filling and 7-day moving averages.
- **Late-Arrival Leaderboard**: Competition-ranked leaderboard handling score ties and limit cutoffs.
- **In-App API Explorer**: Test any backend endpoint directly within the dashboard.

---

## 📋 API Endpoints Reference

| Category | Method | Path | Description & Business Rules |
| :--- | :--- | :--- | :--- |
| **System** | `GET` | `/health` | Live database ping returning `200 OK` if MongoDB answers, `503 Service Unavailable` if unreachable. |
| **Employees** | `POST` | `/employees` | Register employee with strict regex validation on `emp_code`, email format, and `shift_start != shift_end`. |
| | `GET` | `/employees` | Paginated employee list sorted by `emp_code ASC` with accurate filtered total counts. |
| **Attendance** | `POST` | `/attendance/punch-in` | Concurrency-safe punch-in with IST date derivation (**R1**), late arrival math (**R2**), and duplicate prevention (**409 Conflict**). |
| | `POST` | `/attendance/punch-out` | Closes active attendance record, computing overtime (**R3**), work hours (**R4**), and half-day flag (**R5**). |
| | `GET` | `/attendance` | Paginated attendance log sorted by `(date DESC, emp_code ASC)` without loading full collection into RAM. |
| | `PATCH` | `/attendance/{emp_code}/{date}` | Attendance regularization with immutable audit trail and optimistic locking on history length. |
| **Analytics** | `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary (working days, present, leave, late, overtime, % attendance). |
| | `GET` | `/analytics/departments/summary` | Department-level monthly metrics including employees with zero attendance records (**R9**). |
| | `GET` | `/analytics/leaderboard/late` | Competition-ranked late-arrival leaderboard with tie handling and limit cutoffs (**R8**). |
| | `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend with MongoDB `$densify` gap filling and 7-day moving averages (**R10**). |
| **Admin** | `GET` | `/admin/explain/{endpoint}` | MongoDB execution plan (`executionStats`) proving index utilization (`IXSCAN`). |

---

## ⚙️ Core Business Rules (R1–R10)

1. **IST Timezone & Overnight Shifts (R1)**:
   - All timestamps are stored internally as UTC datetimes and serialized to clients as epoch millisecond integers.
   - Attendance dates are assigned in IST (`UTC+05:30`). For overnight shifts (`shift_end <= shift_start`), punches before `shift_end` IST belong to the previous day's shift.
   - Microseconds are truncated before calculating deltas or evaluating thresholds.
2. **Late Arrival Calculation (R2)**:
   - Grace period: arrivals $\le 15$ minutes after shift start are not marked late. Arrivals $> 15$ minutes record the exact minutes past `shift_start`.
3. **Overtime Calculation (R3)**:
   - Overtime is earned only when punching out $\ge 30$ minutes after `shift_end`.
4. **Deterministic Half-Up Rounding (R4, R8)**:
   - All floating-point fields (work hours, percentages, averages) use exact half-up rounding (`ROUND_HALF_UP`) via Python's `Decimal` module rather than banker's rounding.
5. **Half-Day Rule (R5)**:
   - Marked `half_day = true` if `work_hours < 4.50`.
6. **Concurrency Safety**:
   - Punch-in relies on a unique compound MongoDB index on `(emp_code, date)` to prevent race condition duplicates. Duplicate simultaneous requests result in one `201 Created` and `409 Conflict` for all duplicates.
   - Regularization uses optimistic locking matching `history` array length, rejecting concurrent conflicting updates with `409 Conflict`.
7. **Server-Side Aggregations**:
   - All analytical calculations execute inside optimized MongoDB aggregation pipelines (`$lookup`, `$densify`, `$setWindowFields`, indexed `$match`). Never loads full collections into server memory.

---

## 🚀 Installation & Getting Started

### 1. Prerequisites
- **Python**: 3.10 or newer (tested on Python 3.10, 3.11, 3.12).
- **MongoDB**: 6.0 or newer (local MongoDB service or MongoDB Atlas).

### 2. Clone and Setup Environment
```bash
# Clone the repository
git clone <repository_url>
cd candidate_kit

# Create and activate virtual environment
python -m venv .venv

# On Windows:
.venv\Scripts\Activate.ps1
# On Linux / macOS:
source .venv/bin/activate

# Install required dependencies
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Create or verify the `.env` file in the root folder:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Do not commit production database credentials to source control.)*

### 4. Seed the Database
Populate standard employees (`EMP0001`–`EMP0006`) and initial attendance records:
```bash
python sample_seed.py
```

### 5. Start the Application Server
Run the FastAPI application with Uvicorn:
```bash
uvicorn app.main:app --port 8000
```
- **Web UI Dashboard**: [http://localhost:8000/ui/index.html](http://localhost:8000/ui/index.html)
- **Swagger Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)

---

## 🗄️ Database Indexes & Automatic Initialization

On startup, an idempotent handler (`@app.on_event("startup")`) automatically creates all necessary indexes:

- `db.employees`:
  - `{"emp_code": 1}` (**unique**)
  - `{"department": 1, "joined_on": 1}`
  - `{"joined_on": 1}`
- `db.attendance_logs`:
  - `{"emp_code": 1, "date": 1}` (**unique**)
  - `{"date": -1, "emp_code": 1}`
  - `{"emp_code": 1, "date": 1}`
  - `{"date": 1, "late_minutes": 1}`

Index execution can be verified by querying the admin explain endpoint:
```
GET /admin/explain/attendance?emp_code=EMP0001
GET /admin/explain/leaderboard?month=2026-07
GET /admin/explain/departments_summary?month=2026-07
```

---

## 🧪 Automated Test Suite

Run the automated test suites covering all business rules and endpoints:

```bash
# 1. Helper unit tests (time arithmetic, rounding, R1 overnight shifts)
python test_helpers.py

# 2. Punch-out API tests (overtime, half-day, concurrency)
python test_punch_out.py

# 3. Regularization tests (PATCH, audit history, optimistic locking)
python test_regularize.py

# 4. Monthly Employee Analytics tests (working days, presence %, overtime)
python test_monthly_analytics.py

# 5. Department Summary Analytics tests (headcounts, zero-log employees R9)
python test_department_summary.py

# 6. Late-Arrival Leaderboard tests (competition ranking, ties, limit cutoffs)
python test_leaderboard.py

# 7. Department Trend Analytics tests ($densify gap filling, 7-day moving averages)
python test_trend.py

# 8. Admin Explain API tests (MongoDB execution plans, index scans)
python test_explain.py
```

---

## 📁 Repository Structure

```
candidate_kit/
├── app/
│   ├── __init__.py
│   ├── main.py                  # All FastAPI endpoints, helpers, and schemas
│   └── static/
│       └── index.html           # AttendIQ glassmorphic Web UI Dashboard
├── sample_data/
│   ├── employees.json           # Seed employees (EMP0001 - EMP0006)
│   └── attendance_logs.json     # Seed attendance logs
├── .env                         # Local environment configuration
├── .gitignore                   # Git ignore patterns
├── DATA_MODEL.md                # Data model schemas and field definitions
├── DECISIONS.md                 # Design & architecture decisions
├── REVIEW.md                    # Starter code defect review and resolution log
├── openapi.yaml                 # OpenAPI 3.0 specification contract
├── requirements.txt             # Project Python dependencies
├── sample_seed.py               # Database seed runner script
├── test_helpers.py              # Unit tests for time math and rules
├── test_punch_out.py            # Integration tests for punch-out
├── test_regularize.py           # Integration tests for regularization
├── test_monthly_analytics.py    # Integration tests for monthly analytics
├── test_department_summary.py   # Integration tests for department summary
├── test_leaderboard.py          # Integration tests for late leaderboard
├── test_trend.py                # Integration tests for department trends
└── test_explain.py              # Integration tests for MongoDB explain plans
```

---

## 📜 Submission Compliance

- **Single Application File**: All backend code is contained strictly in `app/main.py`.
- **Dependencies**: Clean, pinned in `requirements.txt`.
- **Documentation**: `README.md`, `REVIEW.md`, and `DECISIONS.md` fully completed per specifications.
- **Test Integrity**: All automated test suites execute and pass against the active implementation.
