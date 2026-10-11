
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing. Add it to your .env file.")

client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[MONGO_DB]
employees = db["employees"]
attendance = db["attendance_logs"]

app = FastAPI(title="HROne Attendance & Analytics API", version="1.0.0")

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Accepted range for epoch-millisecond timestamps.
# Anything below the minimum looks like seconds, not milliseconds.
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

LATE_GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW_HOURS = 4.50


# -------------------- STARTUP / INDEXES --------------------

@app.on_event("startup")
def create_indexes():
    client.admin.command("ping")
    employees.create_index([("emp_code", ASCENDING)], unique=True)
    employees.create_index([("department", ASCENDING)])
    attendance.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    attendance.create_index([("date", ASCENDING)])


# -------------------- HELPERS --------------------

def now_utc():
    return datetime.now(timezone.utc)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """Stored datetime -> epoch milliseconds, for API responses."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value - EPOCH) // timedelta(milliseconds=1)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    """Epoch milliseconds -> UTC datetime, cut down to the whole second (R1)."""
    if value is None:
        return None
    return EPOCH + timedelta(seconds=value // 1000)


def punch_instant(punched_at_ms: Optional[int]) -> datetime:
    """The moment of a punch: what the client sent, or right now."""
    if punched_at_ms is None:
        return now_utc().replace(microsecond=0)
    return from_epoch_ms(punched_at_ms)


def round_half_up(value, places: int = 2) -> float:
    step = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Date must use YYYY-MM-DD")


def parse_shift_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def is_overnight_shift(emp: dict) -> bool:
    # "HH:MM" strings are zero-padded, so comparing them as text is safe.
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date_for(emp: dict, punch_in: datetime) -> date:
    """Which attendance day a punch-in belongs to (R1).

    Normally it is the IST calendar date of the punch. On an overnight shift,
    a punch that lands before the shift end (say 00:30 on a 22:00-06:00 shift)
    still belongs to the shift that started the evening before.
    """
    local = punch_in.astimezone(IST)
    if is_overnight_shift(emp) and local.time() < parse_shift_time(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(emp: dict, day: date) -> tuple[datetime, datetime]:
    """Start and end of the employee's shift for one attendance day, in IST."""
    start = datetime.combine(day, parse_shift_time(emp["shift_start"]), tzinfo=IST)
    end = datetime.combine(day, parse_shift_time(emp["shift_end"]), tzinfo=IST)
    if is_overnight_shift(emp):
        end += timedelta(days=1)
    return start, end


def public_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


def public_history_entry(entry: dict) -> dict:
    changes = {}
    for field, change in (entry.get("changes") or {}).items():
        # Punch times sit inside the nested object as datetimes too.
        if field in ("punch_in", "punch_out"):
            change = {
                "from": to_epoch_ms(change.get("from")),
                "to": to_epoch_ms(change.get("to")),
            }
        changes[field] = change
    return {
        "at": to_epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def public_attendance(doc: dict) -> dict:
    """Stored attendance document -> API response shape.

    Older seeded records may not have half_day or history at all, so every
    optional field falls back to its default here.
    """
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [
            public_history_entry(entry) for entry in doc.get("history") or []
        ],
    }


def get_employee(emp_code: str) -> dict:
    employee = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


def calculate_attendance(
    emp: dict,
    day: date,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime] = None,
) -> dict:
    """Work out the derived fields for one attendance day (R2-R5).

    `day` is the attendance date, not the date of the punch, so an overnight
    shift is always measured against the shift it actually belongs to.
    """
    late_minutes = 0
    work_hours = None
    overtime_minutes = 0
    half_day = False

    # ABSENT / LEAVE records have no punches, so there is nothing to compute.
    if punch_in is not None:
        shift_start, shift_end = shift_window(emp, day)

        # R2: late only when strictly more than 10:00 past shift start,
        # and then the minutes count from shift start, not from the grace end.
        seconds_after_start = int((punch_in - shift_start).total_seconds())
        if seconds_after_start > LATE_GRACE_SECONDS:
            late_minutes = seconds_after_start // 60

        if punch_out is not None:
            # R4: hours worked, rounded half-up to 2 decimals.
            worked_seconds = int((punch_out - punch_in).total_seconds())
            work_hours = round_half_up(Decimal(worked_seconds) / Decimal(3600))

            # R3: overtime only counts once it reaches 30 whole minutes.
            minutes_after_end = int((punch_out - shift_end).total_seconds()) // 60
            if minutes_after_end >= MIN_OVERTIME_MINUTES:
                overtime_minutes = minutes_after_end

            # R5: decided on the rounded hours.
            half_day = work_hours < HALF_DAY_BELOW_HOURS

    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def working_days_between(start: date, end: date) -> int:
    if start > end:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


# -------------------- REQUEST MODELS --------------------

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end cannot be equal")
        return self


# A point in time sent by a client: a whole number of milliseconds.
# strict=True rejects strings, floats and booleans with a 422.
EpochMillis = Annotated[
    int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
]


class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOut(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class AttendanceCorrection(BaseModel):
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=3, max_length=500)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def require_change(self):
        if self.punch_in is None and self.punch_out is None:
            raise ValueError("Provide punch_in and/or punch_out")
        return self


# -------------------- HEALTH --------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok", "database": "connected"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable")


# -------------------- EMPLOYEES --------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["created_at"] = now_utc()
    try:
        employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Employee already exists")
    return public_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    total = employees.count_documents(query)
    items = list(
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = to_epoch_ms(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# -------------------- PUNCH IN / OUT --------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    employee = get_employee(body.emp_code)
    punched_at = punch_instant(body.punched_at)
    day = attendance_date_for(employee, punched_at)

    record = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **calculate_attendance(employee, day, punched_at),
        "history": [],
    }

    try:
        attendance.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="Attendance already exists for this employee and date"
        )
    return public_attendance(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOut):
    employee = get_employee(body.emp_code)
    punched_at = punch_instant(body.punched_at)

    day = punched_at.astimezone(IST).date()
    record = attendance.find_one(
        {"emp_code": body.emp_code, "date": day.isoformat()}
    )
    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="Punch-in record not found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")
    if punched_at < record["punch_in"]:
        raise HTTPException(status_code=422, detail="Punch-out cannot precede punch-in")

    metrics = calculate_attendance(
        employee, date.fromisoformat(record["date"]), record["punch_in"], punched_at
    )
    attendance.update_one(
        {"_id": record["_id"]},
        {"$set": {"punch_out": punched_at, **metrics}},
    )
    return public_attendance(attendance.find_one({"_id": record["_id"]}))


# -------------------- MANUAL CORRECTION --------------------

@app.patch("/attendance/{emp_code}/{date_str}")
def correct_attendance(
    emp_code: str,
    date_str: str,
    body: AttendanceCorrection,
):
    employee = get_employee(emp_code)
    day = parse_date(date_str)
    query = {"emp_code": emp_code, "date": day.isoformat()}
    record = attendance.find_one(query)

    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # What is stored now, with the defaults older records may be missing.
    before = {
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day")),
    }

    after = dict(before)
    if body.punch_in is not None:
        after["punch_in"] = from_epoch_ms(body.punch_in)
    if body.punch_out is not None:
        after["punch_out"] = from_epoch_ms(body.punch_out)

    if (
        after["punch_in"] is not None
        and after["punch_out"] is not None
        and after["punch_out"] < after["punch_in"]
    ):
        raise HTTPException(status_code=422, detail="Punch-out cannot precede punch-in")

    after.update(
        calculate_attendance(employee, day, after["punch_in"], after["punch_out"])
    )

    # The audit trail lives on the record itself: one entry per correction,
    # listing only the fields whose value really changed.
    entry = {
        "at": now_utc().replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            field: {"from": before[field], "to": after[field]}
            for field in before
            if before[field] != after[field]
        },
    }
    attendance.update_one(query, {"$set": after, "$push": {"history": entry}})
    return public_attendance(attendance.find_one(query))


# -------------------- ATTENDANCE LIST --------------------

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if department:
        query["department"] = department
    if date_from:
        parse_date(date_from)
        query.setdefault("date", {})["$gte"] = date_from
    if date_to:
        parse_date(date_to)
        query.setdefault("date", {})["$lte"] = date_to

    total = attendance.count_documents(query)
    items = list(
        attendance.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [public_attendance(item) for item in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# -------------------- MONTHLY EMPLOYEE ANALYTICS --------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str):
    employee = get_employee(emp_code)
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")

    next_month = (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )
    start = max(month_start, date.fromisoformat(employee["joined_on"]))
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()},
        }},
        {"$group": {
            "_id": None,
            "days_present": {"$sum": 1},
            "late_days": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            "total_overtime_hours": {"$sum": "$overtime_hours"},
            "half_days": {"$sum": {"$cond": [{"$eq": ["$status", "half_day"]}, 1, 0]}},
        }},
    ]
    rows = list(attendance.aggregate(pipeline))
    result = rows[0] if rows else {
        "days_present": 0, "late_days": 0, "total_late_minutes": 0,
        "total_work_hours": 0, "total_overtime_hours": 0, "half_days": 0,
    }
    result.pop("_id", None)
    result["emp_code"] = emp_code
    result["month"] = month
    result["working_days_since_joining_in_month"] = working_days_between(
        start, min(next_month - timedelta(days=1), now_utc().date())
    )
    return result


# -------------------- DEPARTMENT SUMMARY --------------------

@app.get("/analytics/departments/summary")
def department_summary():
    today = now_utc().astimezone(
        timezone(timedelta(hours=5, minutes=30))
    ).date().isoformat()

    employee_counts = list(employees.aggregate([
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}
    ]))
    daily = list(attendance.aggregate([
        {"$match": {"date": today}},
        {"$group": {
            "_id": "$department",
            "present": {"$sum": 1},
            "late": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
        }},
    ]))
    daily_map = {item["_id"]: item for item in daily}
    result = []
    for item in employee_counts:
        department = item["_id"]
        day_data = daily_map.get(department, {})
        result.append({
            "department": department,
            "headcount": item["headcount"],
            "present": day_data.get("present", 0),
            "late": day_data.get("late", 0),
            "total_late_minutes": day_data.get("total_late_minutes", 0),
            "total_work_hours": day_data.get("total_work_hours", 0),
        })
    return {"date": today, "departments": sorted(result, key=lambda x: x["department"] or "")}


# -------------------- LATE ARRIVAL LEADERBOARD --------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=100),
):
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)

    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": end.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "late_days": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
        }},
        {"$sort": {"late_days": -1, "total_late_minutes": -1, "_id": 1}},
        {
    "$setWindowFields": {
        "sortBy": {"late_days": -1},
        "output": {
            "rank": {"$rank": {}}
        }
    }
},
        {"$limit": limit},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "late_days": 1,
            "total_late_minutes": 1, "rank": 1,
        }},
    ]
    return {"month": month, "items": list(attendance.aggregate(pipeline))}


# -------------------- DEPARTMENT DAILY TREND --------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str,
    date_to: str,
):
    start = parse_date(date_from)
    end = parse_date(date_to)
    if end < start:
        raise HTTPException(status_code=422, detail="date_to must be on or after date_from")
    if (end - start).days > 365:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 366 days")

    pipeline = [
        {"$match": {
            "department": department,
            "date": {"$gte": date_from, "$lte": date_to},
        }},
        {"$group": {
            "_id": "$date",
            "present": {"$sum": 1},
            "late": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    by_day = {row["_id"]: row for row in attendance.aggregate(pipeline)}
    days = []
    current = start
    while current <= end:
        key = current.isoformat()
        row = by_day.get(key, {})
        days.append({
            "date": key,
            "present": row.get("present", 0),
            "late": row.get("late", 0),
            "work_hours": row.get("work_hours", 0),
        })
        current += timedelta(days=1)

    for index, row in enumerate(days):
        window = days[max(0, index - 6):index + 1]
        row["moving_average_7d"] = round(
            sum(item["present"] for item in window) / len(window), 2
        )
    return {"department": department, "items": days}


# -------------------- QUERY EXPLAIN --------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str):
    pipelines = {
        "monthly": [
            {"$match": {"emp_code": "EMP0001", "date": {"$gte": "2026-01-01", "$lt": "2026-02-01"}}},
            {"$group": {"_id": "$emp_code", "days": {"$sum": 1}}},
        ],
        "department-summary": [
            {"$match": {"date": "2026-01-01"}},
            {"$group": {"_id": "$department", "present": {"$sum": 1}}},
        ],
        
"leaderboard": [
    {
        "$group": {
            "_id": "$emp_code",
            "late_days": {
                "$sum": {
                    "$cond": [
                        {"$gt": ["$late_minutes", 0]},
                        1,
                        0,
                    ]
                }
            },
        }
    },
    {"$sort": {"late_days": -1}},
    {"$limit": 10},
],

        "trend": [
            {"$match": {"department": "Engineering", "date": {"$gte": "2026-01-01", "$lte": "2026-01-31"}}},
            {"$group": {"_id": "$date", "present": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ],
    }
    if endpoint not in pipelines:
        raise HTTPException(
            status_code=404,
            detail="Use monthly, department-summary, leaderboard, or trend",
        )

    try:
        result = db.command({
            "explain": {
                "aggregate": "attendance",
                "pipeline": pipelines[endpoint],
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "executionStats": result.get("executionStats", {}),
            "queryPlanner": result.get("queryPlanner", {}),
        }
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Explain failed: {exc}")
