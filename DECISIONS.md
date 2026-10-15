# DECISIONS.md

### 1. Indexes
We created the following indexes at application startup:
- `db.employees`:
  - `emp_code` (unique): Enforces business-key uniqueness and provides $O(1)$ lookups for authentication, punch validation, and joins.
  - `(department, joined_on)` and `joined_on`: Directly support R9 headcount filtering and department summary analytics without collection scans.
- `db.attendance_logs`:
  - `(emp_code, date)` (unique): Guarantees the fundamental business constraint of at most one attendance record per employee per day and eliminates race conditions.
  - `(date DESC, emp_code ASC)`: Matches the default sorting and pagination of `GET /attendance` and date-range filtering.
  - `(emp_code, date ASC)`: Covers employee monthly analytics pipelines (`$match: {emp_code, date}`).
  - `(date ASC, late_minutes ASC)`: Covers the late-arrival leaderboard pipeline (`$match: {date, late_minutes: {$gt: 0}}`).

*Considered and rejected:* We considered an index on `status` alone, but rejected it because low-cardinality fields (5 statuses) yield poor selectivity; compound indexes prefixing high-cardinality `date` or `emp_code` are vastly more efficient.

### 2. Punch-in race
When two identical punch-in requests for the same employee arrive simultaneously:
1. Both requests arrive in FastAPI worker threads, validate the employee, and derive the exact IST attendance date.
2. Both issue an atomic `db.attendance_logs.insert_one()` with the same `(emp_code, date)`.
3. MongoDB's storage engine evaluates the unique compound index `empcode_date_unique`.
4. The first request acquires the index slot, writes the document, and returns `201 Created`.
5. The second concurrent insert violates the unique constraint; MongoDB immediately aborts the insert and raises `pymongo.errors.DuplicateKeyError`.
6. Our endpoint catches `DuplicateKeyError` and cleanly returns `409 Conflict ("already punched in for this date")`. No check-then-insert race condition exists.

### 3. Ties
When two or more employees tie on `total_late_minutes` at the cutoff of `limit`:
- Our aggregation pipeline uses MongoDB `$setWindowFields` with the standard competition ranking function (`$rank`), which assigns identical ranks to identical late minute totals (e.g., ranks 1, 2, 2, 4).
- The pipeline then filters using `{"$match": {"rank": {"$lte": limit}}}`.
- If two employees tie at rank 2 for `limit=2`, both employees satisfy `rank <= limit`. The leaderboard includes both tied employees (returning 3 rows) rather than arbitrarily omitting one.
- Deterministic ordering is guaranteed by sorting by `total_late_minutes DESC, emp_code ASC`. Rank 4 is excluded because its rank strictly exceeds the requested limit.

### 4. Headcount
To ensure employees with zero attendance logs are counted in department summaries:
- The aggregation pipeline begins with `db.employees` as the source collection (rather than `attendance_logs`), filtering with `{"$match": {"joined_on": {"$lte": end_date}}}` per Rule R9.
- It groups by department to compute `headcount: {"$sum": 1}` from all eligible employees regardless of attendance history.
- Attendance logs are joined via `$lookup`. For employees with zero logs in that month, `$lookup` yields an empty list `[]`.
- Flattening these arrays preserves the employee in the department headcount while contributing 0 to present days, and `avg_work_hours` evaluates to `null` if no attendance hours exist.

### 5. One thing you would change for 100x data
At 100x scale (~10 million attendance records):
- Real-time multi-stage aggregations and dynamic cross-collection `$lookup` joins on every analytical request would degrade database latency.
- I would implement incremental pre-aggregated daily/monthly rollups (e.g., maintaining a materialized `monthly_department_summaries` collection updated via change streams or an event-driven queue on punch/regularization events). Additionally, I would shard `attendance_logs` using a compound hashed shard key `(emp_code, date)` to distribute write and read traffic evenly across cluster shards.
