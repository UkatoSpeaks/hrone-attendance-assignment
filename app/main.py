
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

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
audit_logs = db["audit_logs"]

app = FastAPI(title="HROne Attendance & Analytics API", version="1.0.0")


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
    attendance.create_index([("department", ASCENDING), ("date", ASCENDING)])
    audit_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)])


# -------------------- HELPERS --------------------

def now_utc():
    return datetime.now(timezone.utc)


def to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def from_epoch_ms(value: Optional[int]):
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def round_hours(hours: float) -> float:
    return float(
        Decimal(str(hours)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Date must use YYYY-MM-DD")


def shift_datetime(day: date, shift: str) -> datetime:
    hour, minute = map(int, shift.split(":"))
    # Shift schedules are interpreted in IST.
    from datetime import timezone as tz
    ist = tz(timedelta(hours=5, minutes=30))
    return datetime.combine(day, time(hour, minute), tzinfo=ist)


def public_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


def public_attendance(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


def get_employee(emp_code: str) -> dict:
    employee = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee



def calculate_attendance(
    emp: dict,
    punch_in_ms: int,
    punch_out_ms: int | None = None,
) -> dict:
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal, ROUND_HALF_UP

    IST = timezone(timedelta(hours=5, minutes=30))

    def round2(value: float) -> float:
        return float(
            Decimal(str(value)).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

    # Convert epoch milliseconds to IST
    punch_in_utc = datetime.fromtimestamp(
        punch_in_ms / 1000, tz=timezone.utc
    )
    punch_in_ist = punch_in_utc.astimezone(IST)

    # Shift times are interpreted in IST
    shift_start_time = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()
    shift_end_time = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()

    shift_start = datetime.combine(
        punch_in_ist.date(), shift_start_time, tzinfo=IST
    )
    shift_end = datetime.combine(
        punch_in_ist.date(), shift_end_time, tzinfo=IST
    )

    # Handle overnight shifts
    if shift_end <= shift_start:
        shift_end += timedelta(days=1)

    late_minutes = max(
        0,
        int((punch_in_ist - shift_start).total_seconds() // 60),
    )

    # Ten-minute grace applies to late classification.
    is_late = late_minutes > 10

    work_hours = None
    overtime_minutes = 0
    half_day = False

    if punch_out_ms is not None:
        punch_out_utc = datetime.fromtimestamp(
            punch_out_ms / 1000, tz=timezone.utc
        )
        punch_out_ist = punch_out_utc.astimezone(IST)

        # If the punch-in belongs to an overnight shift,
        # the shift end is already on the following day.
        elapsed_seconds = max(
            0,
            (punch_out_ist - punch_in_ist).total_seconds(),
        )
        work_hours = round2(elapsed_seconds / 3600)

        overtime_seconds = max(
            0,
            (punch_out_ist - shift_end).total_seconds(),
        )
        if overtime_seconds >= 30 * 60:
            overtime_minutes = int(overtime_seconds // 60)

        half_day = work_hours < 4.50

    return {
        "late_minutes": late_minutes,
        "is_late": is_late,
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


class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[datetime] = None


class PunchOut(BaseModel):
    emp_code: str
    punched_at: Optional[datetime] = None


class AttendanceCorrection(BaseModel):
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=3, max_length=500)

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

@app.post("/attendance/punch-in")
def punch_in(body: PunchIn):
    employee = get_employee(body.emp_code)
    punched_at = body.punched_at or now_utc()
    if punched_at.tzinfo is None:
        punched_at = punched_at.replace(tzinfo=timezone.utc)

    day = punched_at.astimezone(
        timezone(timedelta(hours=5, minutes=30))
    ).date()
    timestamp = to_epoch_ms(punched_at)

    record = {
        "emp_code": body.emp_code,
        "department": employee["department"],
        "date": day.isoformat(),
        "punch_in": timestamp,
        "punch_out": None,
    }
    record.update(calculate_attendance(employee, day, timestamp, None))

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
    punched_at = body.punched_at or now_utc()
    if punched_at.tzinfo is None:
        punched_at = punched_at.replace(tzinfo=timezone.utc)

    day = punched_at.astimezone(
        timezone(timedelta(hours=5, minutes=30))
    ).date()
    record = attendance.find_one(
        {"emp_code": body.emp_code, "date": day.isoformat()}
    )
    if not record:
        raise HTTPException(status_code=404, detail="Punch-in record not found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    timestamp = to_epoch_ms(punched_at)
    if timestamp < record["punch_in"]:
        raise HTTPException(status_code=422, detail="Punch-out cannot precede punch-in")

    metrics = calculate_attendance(
        employee, day, record["punch_in"], timestamp
    )
    attendance.update_one(
        {"_id": record["_id"]},
        {"$set": {"punch_out": timestamp, **metrics}},
    )
    updated = attendance.find_one({"_id": record["_id"]}, {"_id": 0})
    return updated


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

    new_in = body.punch_in if body.punch_in is not None else record["punch_in"]
    new_out = body.punch_out if body.punch_out is not None else record.get("punch_out")

    if new_out is not None and new_out < new_in:
        raise HTTPException(status_code=422, detail="Punch-out cannot precede punch-in")

    metrics = calculate_attendance(employee, day, new_in, new_out)
    changes = {"punch_in": new_in, "punch_out": new_out, **metrics}

    audit_logs.insert_one({
        "emp_code": emp_code,
        "date": day.isoformat(),
        "reason": body.reason,
        "before": {
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        "after": {"punch_in": new_in, "punch_out": new_out},
        "corrected_at": now_utc(),
    })
    attendance.update_one(query, {"$set": changes})
    return attendance.find_one(query, {"_id": 0})


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
