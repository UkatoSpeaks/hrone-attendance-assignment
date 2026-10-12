
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
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

# anything below the minimum is seconds, not milliseconds
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

LATE_GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW_HOURS = 4.50
MAX_PUNCH_SPAN = timedelta(hours=24)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")


# -------------------- STARTUP / INDEXES --------------------

@app.on_event("startup")
def create_indexes():
    client.admin.command("ping")
    employees.create_index([("emp_code", ASCENDING)], unique=True)
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    employees.create_index([("joined_on", ASCENDING)])
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    attendance.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    # same order as the attendance list sort, so no in-memory sort
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    attendance.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    # punch-out: latest punch-in of an employee
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


# -------------------- HELPERS --------------------

def now_utc():
    return datetime.now(timezone.utc)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value - EPOCH) // timedelta(milliseconds=1)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    # R1: milliseconds are dropped here
    if value is None:
        return None
    return EPOCH + timedelta(seconds=value // 1000)


def punch_instant(punched_at_ms: Optional[int]) -> datetime:
    if punched_at_ms is None:
        return now_utc().replace(microsecond=0)
    return from_epoch_ms(punched_at_ms)


def round_half_up(value, places: int = 2) -> float:
    step = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    # fromisoformat alone also accepts forms like "20260706", so check the shape first.
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
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
    # R1: on an overnight shift, a punch before shift_end (e.g. 00:30 on
    # 22:00-06:00) belongs to the shift that started the previous day
    local = punch_in.astimezone(IST)
    if is_overnight_shift(emp) and local.time() < parse_shift_time(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_window(emp: dict, day: date) -> tuple[datetime, datetime]:
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
        # punch times inside changes are datetimes too
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
    # old seeded records may not have half_day or history
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
    # day is the attendance date, not the date of the punch (overnight shifts)
    late_minutes = 0
    work_hours = None
    overtime_minutes = 0
    half_day = False

    if punch_in is not None:
        shift_start, shift_end = shift_window(emp, day)

        # R2: grace is checked in seconds, minutes count from shift start
        seconds_after_start = int((punch_in - shift_start).total_seconds())
        if seconds_after_start > LATE_GRACE_SECONDS:
            late_minutes = seconds_after_start // 60

        if punch_out is not None:
            worked_seconds = int((punch_out - punch_in).total_seconds())
            work_hours = round_half_up(Decimal(worked_seconds) / Decimal(3600))

            minutes_after_end = int((punch_out - shift_end).total_seconds()) // 60
            if minutes_after_end >= MIN_OVERTIME_MINUTES:
                overtime_minutes = minutes_after_end

            # R5 uses the rounded hours
            half_day = work_hours < HALF_DAY_BELOW_HOURS

    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def validate_punch_out(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise HTTPException(status_code=422, detail="Punch-out must be after punch-in")
    if punch_out - punch_in > MAX_PUNCH_SPAN:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )


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
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must use YYYY-MM-DD")
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end cannot be equal")
        return self


# strict=True so strings, floats and booleans are a 422
EpochMillis = Annotated[
    int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
]


AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOut(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class AttendanceCorrection(BaseModel):
    # derived fields are not listed, so they are ignored if sent
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# -------------------- HEALTH --------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
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

    # looked up by punch time, not by date, so overnight shifts work
    record = attendance.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="Punch-in record not found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")
    validate_punch_out(record["punch_in"], punched_at)

    metrics = calculate_attendance(
        employee, date.fromisoformat(record["date"]), record["punch_in"], punched_at
    )

    # if another request closed the record in between, nothing matches -> 409
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_in": record["punch_in"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **metrics}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out")
    return public_attendance(updated)


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

    before = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day")),
    }

    after = dict(before)
    if body.status is not None:
        after["status"] = body.status
    sent_punch_time = body.punch_in is not None or body.punch_out is not None

    if after["status"] in PRESENCE_STATUSES:
        if body.punch_in is not None:
            after["punch_in"] = from_epoch_ms(body.punch_in)
        if body.punch_out is not None:
            after["punch_out"] = from_epoch_ms(body.punch_out)

        if after["punch_in"] is None:
            raise HTTPException(
                status_code=422,
                detail=f"Status {after['status']} needs a punch_in",
            )
        if (
            body.punch_in is not None
            and attendance_date_for(employee, after["punch_in"]) != day
        ):
            raise HTTPException(
                status_code=422,
                detail="punch_in must stay on the record's attendance date",
            )
        if sent_punch_time and after["punch_out"] is not None:
            validate_punch_out(after["punch_in"], after["punch_out"])
    else:
        # ABSENT / LEAVE: punch times are cleared
        if sent_punch_time:
            raise HTTPException(
                status_code=422,
                detail=f"Status {after['status']} cannot have punch times",
            )
        after["punch_in"] = None
        after["punch_out"] = None

    if all(after[field] == before[field] for field in ("status", "punch_in", "punch_out")):
        raise HTTPException(status_code=422, detail="The request changes nothing")

    after.update(
        calculate_attendance(employee, day, after["punch_in"], after["punch_out"])
    )

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

    # write only if the record is unchanged since we read it (same values,
    # no new history entry); otherwise the slower request gets a 409
    history_length = len(record.get("history") or [])
    updated = attendance.find_one_and_update(
        {
            **query,
            "status": before["status"],
            "punch_in": before["punch_in"],
            "punch_out": before["punch_out"],
            f"history.{history_length}": {"$exists": False},
        },
        {"$set": after, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="The record was changed by someone else, please retry",
        )
    return public_attendance(updated)


# -------------------- ATTENDANCE LIST --------------------

ATTENDANCE_LIST_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    start = parse_date(date_from) if date_from else None
    end = parse_date(date_to) if date_to else None
    if start and end and start > end:
        raise HTTPException(
            status_code=422, detail="date_from cannot be after date_to"
        )

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    # dates are "YYYY-MM-DD" text, which compares the same way as real dates
    if start:
        query.setdefault("date", {})["$gte"] = start.isoformat()
    if end:
        query.setdefault("date", {})["$lte"] = end.isoformat()
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_list_query(emp_code, date_from, date_to, status)

    # no filter: use the collection's counter instead of walking every record
    if query:
        total = attendance.count_documents(query)
    else:
        total = attendance.estimated_document_count()

    cursor = (
        attendance.find(query)
        .sort(ATTENDANCE_LIST_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [public_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# -------------------- MONTHLY EMPLOYEE ANALYTICS --------------------

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_bounds(month: str) -> tuple[date, date]:
    # returns (first day of the month, first day of the next month)
    try:
        year, month_number = map(int, month.split("-"))
        first_day = date(year, month_number, 1)
        if month_number == 12:
            return first_day, date(year + 1, 1, 1)
        return first_day, date(year, month_number + 1, 1)
    except ValueError:
        # e.g. year 0000, which matches the pattern but is not a real date
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")


# shared pieces for the analytics pipelines

IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}

# R7: Mon-Fri. date is stored as text, so convert it to get the weekday
IS_WEEKDAY = {
    "$lte": [
        {"$isoDayOfWeek": {
            "$dateFromString": {
                "dateString": "$date",
                "format": "%Y-%m-%d",
                "onError": None,
                "onNull": None,
            }
        }},
        5,
    ]
}

# R5: half day = 0.5 (missing half_day means false)
PRESENT_DAY_VALUE = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}

IS_LATE = {"$gt": ["$late_minutes", 0]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def employee_monthly_pipeline(emp_code: str, month_start: date, next_month: date) -> list:
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start.isoformat(), "$lt": next_month.isoformat()},
        }},
        {"$group": {
            "_id": None,
            # weekend records don't count here but still add to late/overtime
            "present_days": {"$sum": {
                "$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENT_DAY_VALUE, 0]
            }},
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(pattern=MONTH_PATTERN),
):
    employee = get_employee(emp_code)
    month_start, next_month = month_bounds(month)
    month_end = next_month - timedelta(days=1)

    # R7: mid-month joiners are counted from joined_on
    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = working_days_between(max(month_start, joined_on), month_end)

    rows = list(
        attendance.aggregate(employee_monthly_pipeline(emp_code, month_start, next_month))
    )
    totals = rows[0] if rows else {}
    present_days = totals.get("present_days", 0)

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up(
            Decimal(str(present_days)) * 100 / working_days
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(totals.get("leave_days", 0)),
        "late_count": int(totals.get("late_count", 0)),
        "total_late_minutes": int(totals.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(totals.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


# -------------------- DEPARTMENT SUMMARY --------------------

def department_summary_pipeline(
    month_start: date,
    next_month: date,
    department: Optional[str] = None,
) -> list:
    # runs on employees (not on the logs) so people with zero logs
    # still count in the headcount
    month_end = next_month - timedelta(days=1)

    # R9: joined on or before the last day of the month
    employee_filter = {"joined_on": {"$lte": month_end.isoformat()}}
    if department:
        employee_filter["department"] = department

    has_work_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}

    one_employee_month = [
        {"$match": {
            "date": {"$gte": month_start.isoformat(), "$lt": next_month.isoformat()},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENT_DAY_VALUE, 0]
            }},
            # summed as decimals to avoid float error before rounding
            "work_hours_total": {"$sum": {
                "$cond": [has_work_hours, {"$toDecimal": "$work_hours"}, 0]
            }},
            "work_hours_records": count_if(has_work_hours),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
            "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
        }},
    ]

    return [
        {"$match": employee_filter},
        {"$lookup": {
            "from": attendance.name,
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": one_employee_month,
            "as": "logs",
        }},
        # keep the employee even when there are no logs
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$logs.present_days"},
            # total and count kept separately: average over records,
            # not an average of averages
            "work_hours_total": {"$sum": "$logs.work_hours_total"},
            "work_hours_records": {"$sum": "$logs.work_hours_records"},
            "late_count": {"$sum": "$logs.late_count"},
            "total_late_minutes": {"$sum": "$logs.total_late_minutes"},
            "leave_count": {"$sum": "$logs.leave_count"},
            "on_duty_count": {"$sum": "$logs.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    month_start, next_month = month_bounds(month)
    pipeline = department_summary_pipeline(month_start, next_month, department)

    items = []
    for row in employees.aggregate(pipeline):
        avg_work_hours = None
        if row["work_hours_records"] > 0:
            total = row["work_hours_total"]
            if isinstance(total, Decimal128):
                total = total.to_decimal()
            avg_work_hours = round_half_up(
                Decimal(str(total)) / row["work_hours_records"]
            )

        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": row["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


# -------------------- LATE ARRIVAL LEADERBOARD --------------------

def late_leaderboard_pipeline(
    month_start: date,
    next_month: date,
    limit: int,
    department: Optional[str] = None,
) -> list:
    pipeline = [
        {"$match": {
            "date": {"$gte": month_start.isoformat(), "$lt": next_month.isoformat()},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": employees.name,
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        # logs with no matching employee drop out here, before ranking
        {"$unwind": "$employee"},
    ]

    # filter before ranking, so ranks are inside the department
    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline += [
        # $rank gives 1, 2, 2, 4 for ties
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        # limit is on rank, not row count, so ties at the cutoff are kept
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, next_month = month_bounds(month)
    pipeline = late_leaderboard_pipeline(month_start, next_month, limit, department)
    return {"month": month, "items": list(attendance.aggregate(pipeline))}


# -------------------- DEPARTMENT DAILY TREND --------------------

MAX_TREND_DAYS = 92

# rates are kept as whole ten-thousandths (0.3333 -> 3333) until the end
RATE_SCALE = 10000


def rounded_division(numerator, denominator) -> dict:
    # MongoDB's $round is half-to-even, R8 wants half-up:
    # floor((2n + d) / 2d)
    return {"$floor": {"$divide": [
        {"$add": [{"$multiply": [2, numerator]}, denominator]},
        {"$multiply": [2, denominator]},
    ]}}


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    # runs on employees, since the department lives there. Each employee
    # becomes a list of per-day events (joined, present, late); these are
    # grouped by day and $densify fills the days with nothing on them.
    first_day = start.isoformat()
    last_day = end.isoformat()

    logs_in_range = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
        {"$project": {
            "_id": 0,
            "day": "$date",
            "present": {"$cond": [IS_PRESENT, PRESENT_DAY_VALUE, 0]},
            "late": {"$cond": [IS_LATE, 1, 0]},
        }},
    ]

    # R9: joined before the range -> counted from its first day,
    # joined after the range -> not counted
    join_event = {"$cond": [
        {"$lte": ["$joined_on", last_day]},
        [{"day": {"$max": ["$joined_on", first_day]}, "joined": 1}],
        [],
    ]}

    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": attendance.name,
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": logs_in_range,
            "as": "logs",
        }},
        {"$project": {
            "events": {"$concatArrays": [
                # empty events on both ends so $densify covers the full range
                [{"day": first_day}, {"day": last_day}],
                join_event,
                "$logs",
            ]},
        }},
        {"$unwind": "$events"},
        {"$group": {
            "_id": "$events.day",
            "joined": {"$sum": "$events.joined"},
            "present_count": {"$sum": "$events.present"},
            "late_count": {"$sum": "$events.late"},
        }},
        # $densify needs a real date, not text
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {
            "field": "day",
            "range": {"step": 1, "unit": "day", "bounds": "full"},
        }},
        # headcount = running total of joiners up to that day
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "headcount": {
                    "$sum": "$joined",
                    "window": {"documents": ["unbounded", "current"]},
                },
            },
        }},
        {"$set": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            # days added by $densify have no counts
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {
            # null on weekends or with no headcount; an empty working day is 0
            "rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                rounded_division(
                    {"$multiply": ["$present_count", RATE_SCALE]}, "$headcount"
                ),
                None,
            ]},
        }},
        # this row and up to 6 rows before it; null rates are skipped
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "rate_total": {"$sum": "$rate", "window": {"documents": [-6, 0]}},
                "rate_days": {
                    "$sum": {"$cond": [{"$eq": ["$rate", None]}, 0, 1]},
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$eq": ["$rate", None]},
                None,
                {"$divide": ["$rate", RATE_SCALE]},
            ]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$rate_days", 0]},
                {"$divide": [rounded_division("$rate_total", "$rate_days"), RATE_SCALE]},
                None,
            ]},
        }},
    ]


def trend_range(date_from: str, date_to: str) -> tuple[date, date]:
    start = parse_date(date_from)
    end = parse_date(date_to)
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"Date range cannot exceed {MAX_TREND_DAYS} days",
        )
    return start, end


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
):
    start, end = trend_range(date_from, date_to)
    items = list(
        employees.aggregate(department_trend_pipeline(department, start, end))
    )
    # empty only when the department has no employees
    if not items:
        raise HTTPException(status_code=404, detail="Department not found")
    return {"department": department, "items": items}


# -------------------- QUERY EXPLAIN --------------------

def need(**params):
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(
            status_code=422,
            detail="Missing query parameter(s): " + ", ".join(missing),
        )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # each branch uses the same builder as the real endpoint
    if endpoint == "attendance_list":
        collection = attendance
        command = {
            "find": collection.name,
            "filter": attendance_list_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_LIST_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            collection = attendance
            pipeline = employee_monthly_pipeline(emp_code, *month_bounds(month))

        elif endpoint == "department_summary":
            need(month=month)
            collection = employees
            pipeline = department_summary_pipeline(*month_bounds(month), department)

        elif endpoint == "late_leaderboard":
            need(month=month)
            collection = attendance
            pipeline = late_leaderboard_pipeline(*month_bounds(month), limit, department)

        else:  # department_trend
            need(department=department, **{"from": trend_from, "to": trend_to})
            collection = employees
            start, end = trend_range(trend_from, trend_to)
            pipeline = department_trend_pipeline(department, start, end)

        command = {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}

    plan = db.command("explain", command, verbosity="executionStats")

    # the plan has BSON types (timestamps, binary) that plain JSON can't hold
    return {
        "endpoint": endpoint,
        "collection": collection.name,
        "explain": json.loads(json_util.dumps(plan)),
    }
