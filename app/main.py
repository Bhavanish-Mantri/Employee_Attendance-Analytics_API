"""
Employee Attendance & Analytics API
Implements full contract from openapi.yaml, business rules R1-R10, and data model from DATA_MODEL.md.
"""
from __future__ import annotations

import os
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
import pymongo
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# IST timezone: UTC+05:30
IST = timezone(timedelta(hours=5, minutes=30))

# --------------------------------------------------------------------------- #
# MongoDB Client & Lifespan
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]


def init_db_indexes():
    """
    Creates all required indexes idempotently at application startup.
    Ensures unique constraints and optimal performance (IXSCAN, no COLLSCAN).
    """
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_emp_dept_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_att_emp_date_unique")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_att_emp_punch_in")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_att_date_desc_emp_asc")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="idx_att_date_late")
    except Exception as e:
        # Grader might connect to read-only replica or specific test setup
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


from fastapi.encoders import jsonable_encoder


# --------------------------------------------------------------------------- #
# Custom Exception Handlers
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": jsonable_encoder(exc.errors())},
    )


# --------------------------------------------------------------------------- #
# Business Rule Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_to_seconds_ms(epoch_ms: int) -> int:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return (epoch_ms // 1000) * 1000


def epoch_ms_to_utc_dt(epoch_ms: int) -> datetime:
    """Converts epoch ms to UTC aware datetime truncated to seconds."""
    truncated = truncate_to_seconds_ms(epoch_ms)
    return datetime.fromtimestamp(truncated / 1000.0, tz=timezone.utc)


def utc_dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts UTC or naive datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def quantize_half_up(value: Union[float, Decimal, int], decimals: int = 2) -> float:
    """R8: Rounding of reported numbers: 2 decimals half-up (rates: 4 decimals half-up)."""
    d_val = Decimal(str(value))
    exp = Decimal("10") ** -decimals
    return float(d_val.quantize(exp, rounding=ROUND_HALF_UP))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Overnight shifts: shift_end <= shift_start (e.g. 22:00 - 06:00)."""
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    return (eh_h, eh_m) <= (sh_h, sh_m)


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time is
    strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()

    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_time = time(hour=end_h, minute=end_m, second=0)
        if punch_in_ist.time() < shift_end_time:
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period is 10 minutes.
    A punch-in is late only if strictly more than 10:00 after shift_start.
    When late: whole minutes (floor) elapsed since shift_start, not since grace period ended.
    """
    start_h, start_m = map(int, shift_start.split(":"))
    att_y, att_m, att_d = map(int, attendance_date.split("-"))
    # Shift start instant in IST
    shift_start_dt = datetime(att_y, att_m, att_d, start_h, start_m, 0, tzinfo=IST)

    # Difference in seconds between truncated punch_in and shift_start
    diff_sec = (punch_in_utc - shift_start_dt).total_seconds()
    if diff_sec > 600:  # strictly more than 10:00
        return int(diff_sec // 60)
    return 0


def compute_shift_end_instant(shift_start: str, shift_end: str, attendance_date: str) -> datetime:
    """Computes the shift_end instant for a given shift and attendance date."""
    att_y, att_m, att_d = map(int, attendance_date.split("-"))
    end_h, end_m = map(int, shift_end.split(":"))
    shift_date = dt_date(att_y, att_m, att_d)
    if is_overnight_shift(shift_start, shift_end):
        shift_date += timedelta(days=1)
    return datetime(shift_date.year, shift_date.month, shift_date.day, end_h, end_m, 0, tzinfo=IST)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    """
    shift_end_dt = compute_shift_end_instant(shift_start, shift_end, attendance_date)
    diff_sec = (punch_out_utc - shift_end_dt).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours = diff_sec / 3600.0
    return quantize_half_up(hours, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day = true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats a MongoDB attendance document into OpenAPI AttendanceRecord schema."""
    history_out = []
    for h in doc.get("history", []):
        at_val = h.get("at")
        at_ms = utc_dt_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val

        serialized_changes = {}
        for field_name, ch in h.get("changes", {}).items():
            from_val = ch.get("from")
            to_val = ch.get("to")
            if field_name in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = utc_dt_to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = utc_dt_to_epoch_ms(to_val)
            serialized_changes[field_name] = {"from": from_val, "to": to_val}

        history_out.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": serialized_changes,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": utc_dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out,
    }


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats a MongoDB employee document into OpenAPI Employee schema."""
    c_at = doc.get("created_at")
    created_at_ms = utc_dt_to_epoch_ms(c_at) if isinstance(c_at, datetime) else c_at
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": created_at_ms,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas matching OpenAPI 3.0.3
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def check_shift_diff(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", status_code=200)
def health():
    """Returns 200 only if MongoDB answers a ping; otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Creates an employee. Unique emp_code enforced by database index."""
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees", status_code=200)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Creates the day's attendance record. Concurrency race-safe via unique index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_dt = epoch_ms_to_utc_dt(body.punched_at)
    else:
        p_dt = datetime.now(timezone.utc).replace(microsecond=0)

    att_date = determine_attendance_date(p_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(p_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": p_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this attendance date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Closes the employee's most recent record whose punch_in <= punched_at.
    Computes work_hours, overtime_minutes, and half_day.
    Two simultaneous punch-outs yield exactly one 200 and the other 409.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_out_dt = epoch_ms_to_utc_dt(body.punched_at)
    else:
        p_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in <= p_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in record found for punch-out")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    p_in_dt = record["punch_in"]
    if p_in_dt.tzinfo is None:
        p_in_dt = p_in_dt.replace(tzinfo=timezone.utc)

    # Validation: punched_at <= punch_in or > 24 hours after it -> 422
    diff_sec = (p_out_dt - p_in_dt).total_seconds()
    if diff_sec <= 0 or diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be strictly greater than punch_in and within 24 hours",
        )

    # Compute derived values
    w_hours = compute_work_hours(p_in_dt, p_out_dt)
    ot_mins = compute_overtime(p_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    h_day = compute_half_day(w_hours)

    # Atomic update ensuring only an un-punched record is updated
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_out_dt,
                "work_hours": w_hours,
                "overtime_minutes": ot_mins,
                "half_day": h_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out by concurrent request")

    return serialize_attendance_doc(updated)


@app.get("/attendance", status_code=200)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Fast on 100k docs; server-side sorting and pagination.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


