# DECISIONS.md

1. **Indexes.**
   `employees(emp_code)` unique: duplicate creates get 409.
   `attendance_logs(emp_code, date)` unique: one record per day, plus the monthly summary and the lookups.
   `(date desc, emp_code)` and `(status, date desc, emp_code)`: the attendance list comes out already sorted; the first also serves the leaderboard.
   `(emp_code, punch_in desc)`: finding the record to punch out.
   `employees(joined_on)`, `(department, joined_on)`, `(department, emp_code)`: headcount and the employee list.
   Rejected: `(department, date)` on logs, because logs don't store a department.

2. **Punch-in race.** There is no "does it exist" check. Both requests build the record and insert. The unique `(emp_code, date)` index lets one in; the other gets `DuplicateKeyError`, which I return as 409.

3. **Ties.** Both are returned. `$rank` gives them the same rank and I filter on `rank <= limit`, not on row count, so the list can be longer than `limit`. Tied rows are ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and looks up each person's logs. Someone with no logs is still a row, just with zeros.

5. **100x.** I would stop computing analytics from raw logs on every request and keep a per-day, per-department rollup, updated when a punch or correction is saved.
