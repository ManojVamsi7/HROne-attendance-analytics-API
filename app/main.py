"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Foundation & Helpers
# --------------------------------------------------------------------------- #
def validate_epoch_millis(v: Any) -> Any:
    """
    Validates that a value is strictly an integer epoch millisecond within allowed range.
    Rejects booleans, floats, strings, seconds (< 100_000_000_000), and values > 4_102_444_800_000 with 422.
    """
    if v is None:
        return v
    # In Python, bool is a subclass of int, so reject bool explicitly
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Timestamp must be an integer epoch millisecond; floats, booleans, and strings are not allowed")
    if v < MIN_EPOCH_MS:
        raise ValueError(f"Timestamp looks like seconds or is out of range; must be >= {MIN_EPOCH_MS}")
    if v > MAX_EPOCH_MS:
        raise ValueError(f"Timestamp exceeds maximum range; must be <= {MAX_EPOCH_MS}")
    return v


EpochMillis = Annotated[int, BeforeValidator(validate_epoch_millis)]
EpochMillisNullable = Annotated[Optional[int], BeforeValidator(validate_epoch_millis)]


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncates datetime microsecond precision to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def truncate_ms_to_seconds(ms: int) -> int:
    """Truncates epoch milliseconds to whole seconds (R1)."""
    return (ms // 1000) * 1000


def round_half_up(value: Optional[Union[float, Decimal, int, str]], decimals: int = 2) -> Optional[float]:
    """
    Rounds a number to the specified decimal places using ROUND_HALF_UP (R4, R8).
    Avoids Python's default banker's rounding.
    """
    if value is None:
        return None
    d = Decimal(str(value))
    exp = Decimal("10") ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def to_utc(dt: datetime) -> datetime:
    """Ensures a datetime is timezone-aware in UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_ist(dt: datetime) -> datetime:
    """Converts a UTC or naive datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def now_utc() -> datetime:
    """Returns current UTC time truncated to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def epoch_ms_to_utc_datetime(ms: int) -> datetime:
    """
    Converts epoch milliseconds to a UTC-aware datetime truncated to whole seconds.
    """
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=UTC)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Converts a datetime (UTC, IST, or naive UTC) to epoch milliseconds integer.
    Returns None if dt is None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def serialize_history_entry(entry: dict) -> dict:
    """Serializes a single attendance history entry so all timestamps are epoch ms."""
    res = dict(entry)
    if "at" in res and isinstance(res["at"], datetime):
        res["at"] = datetime_to_epoch_ms(res["at"])
    if "changes" in res and isinstance(res["changes"], dict):
        new_changes = {}
        for field, change in res["changes"].items():
            ch = dict(change)
            if field in ("punch_in", "punch_out"):
                if "from" in ch and isinstance(ch["from"], datetime):
                    ch["from"] = datetime_to_epoch_ms(ch["from"])
                if "to" in ch and isinstance(ch["to"], datetime):
                    ch["to"] = datetime_to_epoch_ms(ch["to"])
            new_changes[field] = ch
        res["changes"] = new_changes
    return res


def serialize_employee(doc: Optional[dict]) -> Optional[dict]:
    """
    Serializes an employee document for API responses:
    - Removes internal MongoDB _id
    - Converts created_at to epoch milliseconds
    """
    if not doc:
        return None
    res = dict(doc)
    res.pop("_id", None)
    if "created_at" in res and isinstance(res["created_at"], datetime):
        res["created_at"] = datetime_to_epoch_ms(res["created_at"])
    return res


def serialize_attendance(doc: Optional[dict]) -> Optional[dict]:
    """
    Serializes an attendance log document for API responses:
    - Removes internal MongoDB _id
    - Converts punch_in and punch_out to epoch milliseconds or null
    - Defaults missing fields (late_minutes, overtime_minutes, half_day, history)
    - Converts nested history dates and punch changes to epoch milliseconds
    """
    if not doc:
        return None
    res = dict(doc)
    res.pop("_id", None)
    res.pop("id", None)
    res["punch_in"] = datetime_to_epoch_ms(res.get("punch_in"))
    res["punch_out"] = datetime_to_epoch_ms(res.get("punch_out"))
    res["late_minutes"] = res.get("late_minutes", 0)
    res["overtime_minutes"] = res.get("overtime_minutes", 0)
    res["half_day"] = bool(res.get("half_day", False))
    raw_history = res.get("history") or []
    res["history"] = [serialize_history_entry(h) for h in raw_history]
    return res


def init_indexes(database) -> None:
    """
    Creates all necessary MongoDB indexes idempotently.
    Safe to call repeatedly on startup.
    """
    database.employees.create_index("emp_code", unique=True)
    database.employees.create_index("department")
    database.employees.create_index("joined_on")
    database.employees.create_index([("department", 1), ("joined_on", 1)])

    database.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True
    )
    database.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)]
    )
    database.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1)]
    )


@app.on_event("startup")
def on_startup():
    try:
        init_indexes(db)
    except Exception as exc:
        print(f"Warning: Could not initialize MongoDB indexes on startup: {exc}")


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Attendance date: IST calendar date of punch_in.
    Overnight exception: if shift_end <= shift_start and punch_in IST time is
    strictly earlier than shift_end, it belongs to the previous day's shift.
    """
    p_ist = to_ist(punch_in_utc)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if p_ist.time() < time(end_h, end_m):
            return (p_ist.date() - timedelta(days=1)).isoformat()
    return p_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """
    R2 Late: Grace period is 10 minutes.
    A punch-in is late only if strictly more than 10:00 (600 seconds) after shift_start.
    When late, whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    p_in_ist = to_ist(truncate_to_seconds(to_utc(punch_in)))
    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    start_h, start_m = map(int, shift_start.split(":"))
    start_ist = datetime.combine(date_obj, time(start_h, start_m), tzinfo=IST)
    diff_sec = (p_in_ist - start_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4 Work hours: (punch_out - punch_in) in seconds / 3600,
    rounded to 2 decimals half-up (ROUND_HALF_UP). Both instants truncated to seconds.
    """
    p_in = truncate_to_seconds(to_utc(punch_in))
    p_out = truncate_to_seconds(to_utc(punch_out))
    diff_sec = (p_out - p_in).total_seconds()
    hours = diff_sec / 3600.0
    return round_half_up(hours, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    """
    R3 Overtime: whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    Overnight shift end falls on the next calendar day.
    """
    p_out_ist = to_ist(truncate_to_seconds(to_utc(punch_out)))
    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        date_obj = date_obj + timedelta(days=1)
    end_ist = datetime.combine(date_obj, time(end_h, end_m), tzinfo=IST)
    diff_min = int((p_out_ist - end_ist).total_seconds() // 60)
    return diff_min if diff_min >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_fields(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMillisNullable = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillisNullable = None


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: EpochMillisNullable = None
    punch_out: EpochMillisNullable = None


def same_instant(dt1: Optional[datetime], dt2: Optional[datetime]) -> bool:
    """Compares two datetimes for instant equality, truncated to whole seconds."""
    if dt1 is None and dt2 is None:
        return True
    if dt1 is None or dt2 is None:
        return False
    return to_utc(truncate_to_seconds(dt1)) == to_utc(truncate_to_seconds(dt2))


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(emp) for emp in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts = epoch_ms_to_utc_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    attendance_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


VALID_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(422, f"Invalid status: {status}")

    if date_from is not None:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(422, "Invalid date_from format; expected YYYY-MM-DD")

    if date_to is not None:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(422, "Invalid date_to format; expected YYYY-MM-DD")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
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
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    ts_out = (
        epoch_ms_to_utc_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    # Find the employee's most recent attendance record with a punch_in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found for employee")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record already punched out")

    p_in = to_utc(record["punch_in"])
    if ts_out <= p_in:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if ts_out > p_in + timedelta(hours=24):
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(p_in, ts_out)
    overtime_minutes = compute_overtime(
        ts_out, emp["shift_end"], record["date"], emp["shift_start"]
    )
    half_day = bool(work_hours < 4.50)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_out,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated_doc is None:
        raise HTTPException(409, "Record already punched out")

    return serialize_attendance(updated_doc)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "Invalid date format; expected YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for date")

    curr_status = record["status"]
    curr_punch_in = to_utc(record["punch_in"]) if record.get("punch_in") is not None else None
    curr_punch_out = to_utc(record["punch_out"]) if record.get("punch_out") is not None else None
    curr_work_hours = record.get("work_hours")
    curr_late_minutes = record.get("late_minutes", 0)
    curr_overtime_minutes = record.get("overtime_minutes", 0)
    curr_half_day = record.get("half_day", False)
    curr_history = record.get("history") or []

    new_status = body.status if body.status is not None else curr_status
    punch_in_provided = body.punch_in is not None
    punch_out_provided = body.punch_out is not None

    if new_status in ("ABSENT", "LEAVE"):
        if punch_in_provided or punch_out_provided:
            raise HTTPException(
                422, "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE"
            )
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        if punch_in_provided:
            target_punch_in = to_utc(epoch_ms_to_utc_datetime(body.punch_in))
        else:
            target_punch_in = curr_punch_in

        if target_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        calc_date = compute_attendance_date(
            target_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date:
            raise HTTPException(
                422,
                f"punch_in belongs to attendance date {calc_date}, but record date is {date}",
            )

        if punch_out_provided:
            target_punch_out = to_utc(epoch_ms_to_utc_datetime(body.punch_out))
        else:
            target_punch_out = curr_punch_out

        if target_punch_out is not None:
            if target_punch_out <= target_punch_in:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if target_punch_out > target_punch_in + timedelta(hours=24):
                raise HTTPException(
                    422, "punch_out cannot be more than 24 hours after punch_in"
                )

        target_late_minutes = compute_late_minutes(
            target_punch_in, emp["shift_start"], date
        )
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_end"], date, emp["shift_start"]
            )
            target_half_day = bool(target_work_hours < 4.50)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    changes = {}
    if new_status != curr_status:
        changes["status"] = {"from": curr_status, "to": new_status}

    if not same_instant(curr_punch_in, target_punch_in):
        changes["punch_in"] = {"from": curr_punch_in, "to": target_punch_in}

    if not same_instant(curr_punch_out, target_punch_out):
        changes["punch_out"] = {"from": curr_punch_out, "to": target_punch_out}

    curr_wh = round_half_up(curr_work_hours, 2)
    target_wh = round_half_up(target_work_hours, 2)
    if curr_wh != target_wh:
        changes["work_hours"] = {"from": curr_wh, "to": target_wh}

    if curr_late_minutes != target_late_minutes:
        changes["late_minutes"] = {"from": curr_late_minutes, "to": target_late_minutes}

    if curr_overtime_minutes != target_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": curr_overtime_minutes,
            "to": target_overtime_minutes,
        }

    if curr_half_day != target_half_day:
        changes["half_day"] = {"from": curr_half_day, "to": target_half_day}

    if not changes:
        raise HTTPException(422, "Correction request produced no changes")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    if "history" not in record or record.get("history") is None:
        update_filter = {
            "_id": record["_id"],
            "$or": [{"history": {"$exists": False}}, {"history": {"$size": 0}}],
        }
    else:
        update_filter = {
            "_id": record["_id"],
            "history": {"$size": len(curr_history)},
        }

    update_op = {
        "$set": {
            "status": new_status,
            "punch_in": target_punch_in,
            "punch_out": target_punch_out,
            "work_hours": target_work_hours,
            "late_minutes": target_late_minutes,
            "overtime_minutes": target_overtime_minutes,
            "half_day": target_half_day,
        },
        "$push": {"history": history_entry},
    }

    updated_doc = db.attendance_logs.find_one_and_update(
        update_filter, update_op, return_document=ReturnDocument.AFTER
    )
    if updated_doc is None:
        raise HTTPException(409, "Concurrent modification conflict")

    return serialize_attendance(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints & Pipeline Builders
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$date"}},
            }
        },
        {
            "$addFields": {
                "day_of_week": {"$dayOfWeek": "$date_obj"},
                "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
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
                                    "$is_present",
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
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


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)

    emp_joined = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    month_start = date(year, m_num, 1)
    month_end = date(year, m_num, last_day)

    effective_start = max(month_start, emp_joined)
    if effective_start > month_end:
        working_days = 0
    else:
        working_days = sum(
            1
            for d in range((month_end - effective_start).days + 1)
            if (effective_start + timedelta(days=d)).weekday() < 5
        )

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        stats = res[0]
        present_days = stats.get("present_days", 0.0)
        leave_days = stats.get("leave_days", 0)
        late_count = stats.get("late_count", 0)
        total_late_minutes = stats.get("total_late_minutes", 0)
        total_overtime_minutes = stats.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2) or 0.0,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_stage = {"joined_on": {"$lte": end_date}}
    if department is not None:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$emp_codes"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "date_obj": {"$dateFromString": {"dateString": "$date"}}
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {"$dayOfWeek": "$date_obj"},
                            "is_present": {
                                "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                            },
                            "has_work_hours": {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
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
                                                "$is_present",
                                                {"$gte": ["$day_of_week", 2]},
                                                {"$lte": ["$day_of_week", 6]},
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
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                                }
                            },
                            "avg_work_hours": {
                                "$avg": {
                                    "$cond": [
                                        "$has_work_hours",
                                        "$work_hours",
                                        "$$REMOVE",
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "stats": {"$arrayElemAt": ["$stats", 0]},
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        stats = r.get("stats") or {}
        avg_wh = stats.get("avg_work_hours")
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(stats.get("present_days", 0.0), 2) or 0.0,
            "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
            "late_count": int(stats.get("late_count", 0)),
            "total_late_minutes": int(stats.get("total_late_minutes", 0)),
            "leave_count": int(stats.get("leave_count", 0)),
            "on_duty_count": int(stats.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    year, m_num = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m_num)
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

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
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in results
    ]

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_: str, to: str) -> list:
    try:
        d_from = datetime.strptime(from_, "%Y-%m-%d").date()
        d_to = datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(422, "Invalid date format; expected YYYY-MM-DD")

    if d_to < d_from:
        raise HTTPException(422, "to date must be >= from date")

    day_count = (d_to - d_from).days + 1
    if day_count > 92:
        raise HTTPException(422, f"Date range of {day_count} days exceeds maximum allowed 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    d_from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    d_to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)

    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_, "$lte": to},
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
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_obj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [d_from_dt, d_to_dt_plus_1],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_obj"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$day_of_week", 2]},
                        {"$lte": ["$day_of_week", 6]},
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_doc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_doc.count", 0]}, 0]}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
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
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_, to)
    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in rows:
        rate = r.get("attendance_rate")
        ma = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2) or 0.0,
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(ma, 4) if ma is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
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
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain_res),
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain_res),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": jsonable_encoder(explain_res),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain_res),
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, "department is required for department_trend")
        if not from_:
            raise HTTPException(422, "from is required for department_trend")
        if not to:
            raise HTTPException(422, "to is required for department_trend")
        pipeline = build_department_trend_pipeline(department, from_, to)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_res = db.command("explain", cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain_res),
        }

    raise HTTPException(422, f"Unsupported endpoint: {endpoint}")
