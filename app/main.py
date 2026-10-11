
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

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

# Accepted range for epoch-millisecond timestamps.
# Anything below the minimum looks like seconds, not milliseconds.
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
    # Employee list filtered by department, already in emp_code order.
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    # Headcount: who had joined by a given day, overall or in one department.
    employees.create_index([("joined_on", ASCENDING)])
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    attendance.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    # The attendance list is sorted by date (newest first), then emp_code.
    # These two let MongoDB read rows already in that order, with or
    # without a status filter, instead of sorting them itself.
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    attendance.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    # Punch-out looks up an employee's latest punch-in.
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


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


AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOut(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class AttendanceCorrection(BaseModel):
    # Derived fields are not listed here, so if a client sends them
    # they are simply ignored.
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

    # Close the latest record that started at or before this punch. Finding it
    # by punch time instead of by today's date is what makes overnight shifts
    # work: their punch-out lands on the next calendar day.
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

    # The filter repeats what we just read. If another request closed or
    # corrected the record in the meantime, nothing matches and we lose the race.
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

    # What is stored now, with the defaults older records may be missing.
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
        # A correction fixes the time of a day; it cannot move the record to another day.
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
        # ABSENT and LEAVE mean nobody punched, so the times are wiped.
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

    # Only write if the record is still exactly as we read it: same status and
    # punches, and no history entry added since. Two people correcting the same
    # day at once cannot overwrite each other; the slower one gets a 409.
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
    """Build the MongoDB filter for the attendance list from its query params."""
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
    # Dates are stored as "YYYY-MM-DD" text, which sorts the same way as real dates.
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

    # With no filter, the collection's own counter is instant;
    # counting matches would walk every record.
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
    """First day of the month and first day of the month after it."""
    try:
        year, month_number = map(int, month.split("-"))
        first_day = date(year, month_number, 1)
        if month_number == 12:
            return first_day, date(year + 1, 1, 1)
        return first_day, date(year, month_number + 1, 1)
    except ValueError:
        # e.g. year 0000, which matches the pattern but is not a real date
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")


# Small building blocks shared by the analytics pipelines. They only read
# the stored values; nothing is recomputed from the punch times.

# R6: these statuses count as being present.
IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}

# R7: the record's date falls on Monday-Friday. `date` is stored as text,
# so it is turned into a real date just to ask which day of the week it is.
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

# R5: a half day is worth 0.5 of a present day. A missing half_day means false.
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
            # Weekend records are left out of present_days,
            # but still add to the late and overtime totals below.
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

    # R7: someone who joined mid-month is only expected from their joining day.
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
    """Runs on `employees`, not on the logs.

    Starting from the employees is what keeps people with zero logs in the
    headcount: every employee produces a row, and their month of logs (possibly
    none) is attached to it.
    """
    month_end = next_month - timedelta(days=1)

    # R9: everyone who had joined by the last day of the month.
    employee_filter = {"joined_on": {"$lte": month_end.isoformat()}}
    if department:
        employee_filter["department"] = department

    # The average only looks at present records that have hours recorded.
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
            # Hours are added up as exact decimals, so the average is not
            # thrown off by floating-point error before it gets rounded.
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
        # Keep the employee even when the lookup found nothing.
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$logs.present_days"},
            # Totals and record counts are summed across the department, so the
            # average is over records, not an average of each person's average.
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
        # Only late records matter here, which also means anyone whose
        # total would be 0 never appears.
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
        # Logs that belong to no known employee drop out here, before
        # ranking, so they cannot take up a rank.
        {"$unwind": "$employee"},
    ]

    # Filtering before the ranking step makes the ranks restart inside the department.
    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline += [
        # $rank is competition ranking: ties share a rank and the next
        # one is skipped (1, 2, 2, 4).
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        # The limit is on the rank, not on the number of rows, so everyone
        # tied at the cutoff is kept.
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
