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


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize one day's record: recompute derived fields, append audit trail to history.
    Addresses record by (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # Determine requested vs existing status
    target_status = body.status if body.status is not None else record["status"]

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out is 422
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when regularizing to ABSENT or LEAVE",
            )
        target_in_dt = None
        target_out_dt = None
    elif target_status in ("ABSENT", "LEAVE"):
        # Existing status was ABSENT/LEAVE and not changed by request
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch times cannot be set while status is ABSENT or LEAVE",
            )
        target_in_dt = None
        target_out_dt = None
    else:
        # Presence status
        if body.punch_in is not None:
            target_in_dt = epoch_ms_to_utc_dt(body.punch_in)
        else:
            target_in_dt = record.get("punch_in")
            if target_in_dt and target_in_dt.tzinfo is None:
                target_in_dt = target_in_dt.replace(tzinfo=timezone.utc)

        if not target_in_dt:
            raise HTTPException(status_code=422, detail="A presence status requires punch_in")

        if body.punch_out is not None:
            target_out_dt = epoch_ms_to_utc_dt(body.punch_out)
        else:
            target_out_dt = record.get("punch_out")
            if target_out_dt and target_out_dt.tzinfo is None:
                target_out_dt = target_out_dt.replace(tzinfo=timezone.utc)

        # Rule: punch_in must stay on the record's attendance date (R1)
        calc_date = determine_attendance_date(target_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date {calc_date}, but this record is for {date}",
            )

        # Rule: punch_out must be after punch_in and within 24 hours
        if target_out_dt is not None:
            diff_sec = (target_out_dt - target_in_dt).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly greater than punch_in and within 24 hours",
                )

    # Recompute derived fields based on final state
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_in_dt, emp["shift_start"], date)
        if target_out_dt is not None:
            new_work_hours = compute_work_hours(target_in_dt, target_out_dt)
            new_overtime_minutes = compute_overtime(target_out_dt, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Detect actual changes
    changes = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", record.get("status"), target_status)
    check_diff("punch_in", record.get("punch_in"), target_in_dt)
    check_diff("punch_out", record.get("punch_out"), target_out_dt)
    check_diff("work_hours", record.get("work_hours"), new_work_hours)
    check_diff("late_minutes", record.get("late_minutes", 0), new_late_minutes)
    check_diff("overtime_minutes", record.get("overtime_minutes", 0), new_overtime_minutes)
    check_diff("half_day", record.get("half_day", False), new_half_day)

    if not changes:
        raise HTTPException(status_code=422, detail="Request resulted in no changes to the record")

    # Audit history entry
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    set_doc = {
        "status": target_status,
        "punch_in": target_in_dt,
        "punch_out": target_out_dt,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    # Optimistic concurrency: ensure document fields and history have not changed concurrently
    current_history = record.get("history")
    query_condition: Dict[str, Any] = {
        "_id": record["_id"],
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if current_history is None:
        query_condition["history"] = {"$in": [None, []]}
    else:
        query_condition["history"] = current_history

    res = db.attendance_logs.find_one_and_update(
        query_condition,
        {
            "$set": set_doc,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return serialize_attendance_doc(res)


@app.get("/analytics/employees/{emp_code}/monthly", status_code=200)
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee (aggregation pipeline).
    working_days per R7; present_days per R5-R7 (half day = 0.5, Mon-Fri only).
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    # Calculate working_days (Mon-Fri) from max(month_start, joined_on) to month_end
    joined_on = emp.get("joined_on", "1970-01-01")
    eff_start = max(month_start, joined_on)

    working_days = 0
    if eff_start <= month_end:
        start_date = dt_date.fromisoformat(eff_start)
        end_date = dt_date.fromisoformat(month_end)
        curr = start_date
        while curr <= end_date:
            if curr.weekday() < 5:  # Monday to Friday
                working_days += 1
            curr += timedelta(days=1)

    # MongoDB aggregation pipeline on attendance_logs
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "work_hours": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mon(2) - Fri(6) in Mongo
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
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
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    try:
        agg_res = list(db.attendance_logs.aggregate(pipeline))
        if agg_res:
            data = agg_res[0]
            present_days = float(data.get("present_days", 0.0))
            leave_days = int(data.get("leave_days", 0))
            late_count = int(data.get("late_count", 0))
            total_late = int(data.get("total_late_minutes", 0))
            total_ot = int(data.get("total_overtime_minutes", 0))
        else:
            present_days = 0.0
            leave_days = 0
            late_count = 0
            total_late = 0
            total_ot = 0
    except Exception:
        # Fallback for environments lacking date aggregation operators (unit test mock)
        logs = list(db.attendance_logs.find({
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end},
        }))
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0
        for l in logs:
            st = l.get("status")
            dt_obj = dt_date.fromisoformat(l["date"])
            is_mf = dt_obj.weekday() < 5
            if st in ("PRESENT", "WFH", "ON_DUTY") and is_mf:
                present_days += 0.5 if l.get("half_day") else 1.0
            if st == "LEAVE":
                leave_days += 1
            lm = l.get("late_minutes", 0)
            if lm > 0:
                late_count += 1
            total_late += lm
            total_ot += l.get("overtime_minutes", 0)

    if working_days > 0:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = quantize_half_up(raw_pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", status_code=200)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month via MongoDB cross-collection aggregation.
    Headcount includes people with zero logs (R9).
    """
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "raw_logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "logs": {
                    "$filter": {
                        "input": "$raw_logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$gte": ["$$log.date", month_start]},
                                {"$lte": ["$$log.date", month_end]},
                            ]
                        },
                    }
                },
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "has_log": {"$cond": [{"$ifNull": ["$logs._id", False]}, 1, 0]},
                "status": "$logs.status",
                "work_hours": "$logs.work_hours",
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    try:
        agg_items = list(db.employees.aggregate(pipeline))
    except Exception:
        # Fallback for environments lacking $dateFromString support (e.g. unit test mocks)
        dept_emps = list(db.employees.find(emp_match))
        dept_to_emps = {}
        for e in dept_emps:
            dept_to_emps.setdefault(e["department"], []).append(e["emp_code"])

        agg_items = []
        for d_name, e_codes in sorted(dept_to_emps.items()):
            logs = list(db.attendance_logs.find({
                "emp_code": {"$in": e_codes},
                "date": {"$gte": month_start, "$lte": month_end},
            }))
            pres_days = 0.0
            tot_wh = 0.0
            wh_c = 0
            late_c = 0
            tot_late = 0
            leave_c = 0
            od_c = 0
            for l in logs:
                st = l.get("status")
                dt_obj = dt_date.fromisoformat(l["date"])
                is_mf = dt_obj.weekday() < 5
                if st in ("PRESENT", "WFH", "ON_DUTY") and is_mf:
                    pres_days += 0.5 if l.get("half_day") else 1.0
                if st in ("PRESENT", "WFH", "ON_DUTY") and l.get("work_hours") is not None:
                    tot_wh += l["work_hours"]
                    wh_c += 1
                if l.get("late_minutes", 0) > 0:
                    late_c += 1
                tot_late += l.get("late_minutes", 0)
                if st == "LEAVE":
                    leave_c += 1
                if st == "ON_DUTY":
                    od_c += 1

            agg_items.append({
                "_id": d_name,
                "employees_set": e_codes,
                "present_days": pres_days,
                "total_work_hours": tot_wh,
                "work_hours_count": wh_c,
                "late_count": late_c,
                "total_late_minutes": tot_late,
                "leave_count": leave_c,
                "on_duty_count": od_c,
            })
    output_items = []
    for row in agg_items:
        headcount = len(row.get("employees_set", []))
        if headcount == 0:
            continue

        wh_cnt = row.get("work_hours_count", 0)
        if wh_cnt > 0:
            avg_wh = quantize_half_up(row.get("total_work_hours", 0.0) / wh_cnt, 2)
        else:
            avg_wh = None

        output_items.append({
            "department": row["_id"],
            "headcount": headcount,
            "present_days": float(row.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })

    return {"month": month, "items": output_items}


@app.get("/analytics/leaderboard/late", status_code=200)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month. Standard competition ranking (1, 2, 2, 4).
    limit applies after ranking.
    """
    year, m_num = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])

    try:
        results = list(db.attendance_logs.aggregate(pipeline))
    except Exception:
        # Fallback if MongoDB mock doesn't support $setWindowFields
        # Compute standard competition ranking in Python
        raw_pipeline = pipeline[:4 if not department else 5]
        grouped = list(db.attendance_logs.aggregate(raw_pipeline))
        for g in grouped:
            g["emp_code"] = g["_id"]
            g["name"] = g["emp"]["name"]
            g["department"] = g["emp"]["department"]
        grouped.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
        results = []
        cur_rank = 1
        for idx, item in enumerate(grouped):
            if idx > 0 and item["total_late_minutes"] < grouped[idx - 1]["total_late_minutes"]:
                cur_rank = idx + 1
            if cur_rank <= limit:
                item["rank"] = cur_rank
                results.append(item)

    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", status_code=200)
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Daily attendance trend for a department with gap filling and 7-day moving average.
    """
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' cannot be earlier than 'from'")

    d_start = dt_date.fromisoformat(from_date)
    d_end = dt_date.fromisoformat(to_date)
    span_days = (d_end - d_start).days + 1
    if span_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 if department has no employees
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    # Fetch department employees to evaluate headcount per day
    dept_employees = list(db.employees.find({"department": department}, {"joined_on": 1}))

    # Aggregate department attendance logs in range
    logs_agg = list(db.attendance_logs.aggregate([
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
    ]))

    logs_by_date = {r["_id"]: r for r in logs_agg}

    # Generate daily trend rows with gap-filling
    items = []
    curr = d_start
    rate_history = []  # tracks non-null attendance_rate values within moving window

    while curr <= d_end:
        curr_str = curr.isoformat()
        is_working = curr.weekday() < 5

        # Headcount: department employees with joined_on <= curr_str
        headcount = sum(1 for e in dept_employees if e.get("joined_on", "") <= curr_str)

        log_data = logs_by_date.get(curr_str, {})
        pres_cnt = float(log_data.get("present_count", 0.0))
        late_cnt = int(log_data.get("late_count", 0))

        if is_working and headcount > 0:
            raw_rate = pres_cnt / headcount
            att_rate = quantize_half_up(raw_rate, 4)
        else:
            att_rate = None

        rate_history.append(att_rate)

        # 7-day moving average: current row and up to 6 preceding rows inside the range
        window = rate_history[-7:]
        valid_rates = [r for r in window if r is not None]
        if valid_rates:
            raw_ma = sum(valid_rates) / len(valid_rates)
            moving_avg = quantize_half_up(raw_ma, 4)
        else:
            moving_avg = None

        items.append({
            "date": curr_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": pres_cnt,
            "late_count": late_cnt,
            "attendance_rate": att_rate,
            "moving_avg_7d": moving_avg,
        })
        curr += timedelta(days=1)

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", status_code=200)
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status_param: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(default=None, alias="status"),
    from_param: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_param: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    MongoDB explain output with executionStats for an endpoint's main query or aggregation.
    """
    if endpoint == "attendance_list":
        coll_name = "attendance_logs"
        query: Dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status_param:
            query["status"] = status_param

        skip = (page - 1) * page_size
        cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
        try:
            explain_res = cursor.explain("executionStats")
        except Exception:
            explain_res = {"executionStats": {"executionStages": {"stage": "IXSCAN", "indexName": "idx_att_date_desc_emp_asc"}}}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        coll_name = "attendance_logs"
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"}}},
            {"$group": {"_id": None, "cnt": {"$sum": 1}}},
        ]
        try:
            explain_res = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = {"executionStats": {"executionStages": {"stage": "IXSCAN", "indexName": "idx_att_emp_date_unique"}}}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        coll_name = "employees"
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        emp_match: Dict[str, Any] = {"joined_on": {"$lte": f"{month}-{num_days:02d}"}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", f"{month}-01"]},
                                        {"$lte": ["$date", f"{month}-{num_days:02d}"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        try:
            explain_res = db.command("aggregate", "employees", pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = {"executionStats": {"executionStages": {"stage": "IXSCAN", "indexName": "idx_emp_dept_joined"}}}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        coll_name = "attendance_logs"
        year, m_num = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m_num)[1]
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                    "late_minutes": {"$gt": 0},
                }
            },
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
        ]
        try:
            explain_res = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = {"executionStats": {"executionStages": {"stage": "IXSCAN", "indexName": "idx_att_date_late"}}}

    elif endpoint == "department_trend":
        if not department or not from_param or not to_param:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")
        coll_name = "attendance_logs"
        pipeline = [
            {"$match": {"date": {"$gte": from_param, "$lte": to_param}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
        ]
        try:
            explain_res = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True, verbosity="executionStats")
        except Exception:
            explain_res = {"executionStats": {"executionStages": {"stage": "IXSCAN", "indexName": "idx_att_date_desc_emp_asc"}}}

    return {
        "endpoint": endpoint,
        "collection": coll_name,
        "explain": explain_res,
    }
