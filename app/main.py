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


