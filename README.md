# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne engineering assignment. All the code is in `app/main.py`.

## Run it

Needs Python 3.11+ and MongoDB 6.0+ (I used Python 3.12 and MongoDB Atlas, version 8.0).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then put your own values in it
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment. A local `.env` is loaded for convenience, but real environment variables take priority. The app refuses to start if `MONGO_URI` is missing.

Indexes are created on startup, so there is nothing to set up by hand. `GET /health` returns 200 once MongoDB answers.

Interactive docs are at `http://localhost:8000/docs`.

## Other files

- `REVIEW.md`: the defects I found in the starter and how I fixed them.
- `DECISIONS.md`: answers to the five questions.

## Things worth knowing

Everything in `openapi.yaml` is implemented. A few places in the contract can be read two ways, so this is what I went with:

- **Punch-out earlier than the punch-in.** The contract says to close the latest record whose `punch_in` is at or before `punched_at`. A punch-out that is earlier than every punch-in finds no such record, so it returns 404. Equal times return 422.
- **Punch-out on a record that is already closed.** Returns 409, and this is checked before the 24-hour rule.
- **Trend range limit.** "Exceeds 92 days" is counted inclusively: 1 July to 30 September is 92 rows and is allowed, one more day is 422.
- **Moving average.** It is the mean of the reported (already rounded) `attendance_rate` values in the window.
- **Records before `joined_on`.** They are not filtered out of the monthly and department numbers. Only `working_days` and headcount look at `joined_on`.
- **Department summary with an unknown `department`.** Returns 200 with an empty `items` list, since the contract has no 404 for that endpoint.
- **Concurrent corrections.** If two PATCH requests hit the same record at the same moment, one succeeds and the other gets 409 and can retry. No history entry is lost.

## Testing

There is no test suite in the repo. I tested by running the app against MongoDB and calling the endpoints with my own data: ties on the leaderboard, employees with no logs, weekend records, mid-month joiners, overnight shifts, missing days, old records without `half_day` / `history`, and parallel punch-ins, punch-outs and corrections. I also checked each explain plan for `IXSCAN` and no `COLLSCAN`.
