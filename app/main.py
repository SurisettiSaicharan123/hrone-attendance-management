
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException, Query




load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017")
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------------------------------------------------------------------------
# Time and calculation helpers
# ---------------------------------------------------------------------------

def truncate_seconds(value: datetime) -> datetime:
    """Truncate fractional seconds."""
    return value.replace(microsecond=0)


def parse_instant(epoch_ms: Optional[int]) -> datetime:
    """Convert epoch milliseconds to a whole-second UTC instant."""
    if epoch_ms is None:
        value = datetime.now(UTC)
    else:
        value = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)

    return truncate_seconds(value)


def to_ist(value: datetime) -> datetime:
    """Convert a stored MongoDB datetime to IST."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def to_mongo_utc(value: datetime) -> datetime:
    """MongoDB stores UTC as a naive datetime by default."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return truncate_seconds(value.astimezone(UTC)).replace(tzinfo=None)


def output_datetime(value):
    """Return a stored datetime as an ISO-8601 UTC timestamp."""
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return truncate_seconds(value).isoformat()


def shift_datetimes(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
):
    """Return the scheduled shift start and end in IST."""
    day = date.fromisoformat(attendance_date)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start = datetime(
        day.year, day.month, day.day,
        start_h, start_m, tzinfo=IST,
    )
    end = datetime(
        day.year, day.month, day.day,
        end_h, end_m, tzinfo=IST,
    )

    if end <= start:
        end += timedelta(days=1)

    return start, end


def attendance_shift_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """Determine the shift's starting date for an overnight shift."""
    local_in = to_ist(punch_in)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    punch_minutes = local_in.hour * 60 + local_in.minute

    day = local_in.date()

    if end_minutes <= start_minutes and punch_minutes < end_minutes:
        day -= timedelta(days=1)

    return day.isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    start, _ = shift_datetimes(
        attendance_date, shift_start, shift_end
    )
    local_in = to_ist(punch_in)
    difference = int((local_in - start).total_seconds())

    # Late only when strictly more than 10 minutes.
    return difference // 60 if difference > 600 else 0


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)

    rounded = hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(rounded)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    _, scheduled_end = shift_datetimes(
        attendance_date, shift_start, shift_end
    )

    local_out = to_ist(punch_out)
    difference = int((local_out - scheduled_end).total_seconds())

    # Overtime is counted only when it reaches 30 whole minutes.
    minutes = difference // 60
    return minutes if minutes >= 30 else 0


def serialize_employee(employee: dict) -> dict:
    result = dict(employee)
    result.pop("_id", None)

    created_at = result.get("created_at")
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        result["created_at"] = int(created_at.timestamp() * 1000)

    return result


def serialize_attendance(record: dict) -> dict:
    result = dict(record)
    result["id"] = str(result.pop("_id", result.get("id", "")))

    for field in ("punch_in", "punch_out"):
        result[field] = output_datetime(result.get(field))

    return result


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")

        if parsed.isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")

        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("Shift start and end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is not None and value not in {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }:
            raise ValueError("Invalid attendance status")
        return value




# ---------------------------------------------------------------------------
# Startup indexes
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        name="ix_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="ix_attendance_employee_punch_in",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(tzinfo=None)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department:
        query["department"] = department

    total = db.employees.count_documents(query)

    docs = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(item) for item in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Punch in
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    if body.status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    instant = parse_instant(body.punched_at)
    local_in = instant.astimezone(IST)

    attendance_date = attendance_shift_date(
        instant,
        employee["shift_start"],
        employee["shift_end"],
    )

    # Reject duplicate attendance for this employee and shift date.
    existing = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": attendance_date,
    })

    if existing:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this attendance date",
        )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": to_mongo_utc(instant),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            instant,
            employee["shift_start"],
            employee["shift_end"],
            attendance_date,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    result = db.attendance_logs.insert_one(doc)
    doc["_id"] = result.inserted_id

    return serialize_attendance(doc)


# ---------------------------------------------------------------------------
# Punch out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    instant = parse_instant(body.punched_at)
    punch_out_utc = to_mongo_utc(instant)

    # Select the newest eligible record, even if it is already closed.
    # This prevents incorrectly skipping a closed recent record.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code},
        sort=[("punch_in", -1)],
        )
    if record is not None:
        stored_punch_in = record["punch_in"]
        if stored_punch_in.tzinfo is None:
            stored_punch_in = stored_punch_in.replace(tzinfo=UTC)
        if stored_punch_in > instant:
            record = None













    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found before the requested punch-out time",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="This attendance record is already punched out",
        )

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    if instant <= punch_in_utc:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in",
        )

    duration_seconds = int((instant - punch_in_utc).total_seconds())

    if duration_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(instant_from_mongo(record["punch_in"]), instant)
    overtime_minutes = compute_overtime(
        instant,
        employee["shift_start"],
        employee["shift_end"],
        record["date"],
    )
    half_day = work_hours < 4.50

    # Atomic conditional update: only one concurrent request can succeed.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="This attendance record has already been punched out",
        )

    return serialize_attendance(updated)


def instant_from_mongo(value: datetime) -> datetime:
    """Convert MongoDB's stored UTC datetime into an aware UTC instant."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Attendance listing
# ---------------------------------------------------------------------------

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        query["status"] = status

    if date_from or date_to:
        date_filter = {}

        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must be YYYY-MM-DD",
                )
            date_filter["$gte"] = date_from

        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="date_to must be YYYY-MM-DD",
                )
            date_filter["$lte"] = date_to

        query["date"] = date_filter

    print("DEBUG database:", db.name)
    print("DEBUG attendance count:", db.attendance_logs.count_documents({}))    

    total = db.attendance_logs.count_documents(query)

    records = list(
        db.attendance_logs.find(query)
        .sort([("date", -1), ("punch_in", -1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(item) for item in records],
        "total": total,
        "page": page,
        "page_size": page_size,
    }



# ---------------------------------------------------------------------------
# Attendance regularization (manual correction)
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # Validate the attendance date.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be YYYY-MM-DD",
        )

    # Find the employee.
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Find the record using its natural key.
    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found for this date",
        )

    # Preserve the distinction between omitted fields and fields supplied
    # explicitly in the request.
    supplied = body.model_fields_set

    if not ({"status", "punch_in", "punch_out"} & supplied):
        raise HTTPException(
            status_code=422,
            detail="At least one attendance field must be changed",
        )

    # ABSENT and LEAVE cannot be supplied together with punch times,
    # even if a supplied punch time is null.
    requested_status = body.status if "status" in supplied else record["status"]

    if requested_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="Do not supply punch times with ABSENT or LEAVE",
            )

        new_punch_in = None
        new_punch_out = None
    else:
        old_punch_in = record.get("punch_in")
        old_punch_out = record.get("punch_out")

        new_punch_in = (
            parse_instant(body.punch_in)
            if "punch_in" in supplied and body.punch_in is not None
            else instant_from_mongo(old_punch_in)
            if old_punch_in is not None
            else None
        )

        new_punch_out = (
            parse_instant(body.punch_out)
            if "punch_out" in supplied and body.punch_out is not None
            else instant_from_mongo(old_punch_out)
            if old_punch_out is not None
            else None
        )

        # A presence status requires a punch-in.
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires punch_in",
            )

        # The punch-in must belong to this attendance shift date.
        calculated_date = attendance_shift_date(
            new_punch_in,
            employee["shift_start"],
            employee["shift_end"],
        )
        if calculated_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the record's attendance date",
            )

        # If a punch-out exists, it must follow punch-in and be within 24h.
        if new_punch_out is not None:
            duration = (new_punch_out - new_punch_in).total_seconds()

            if duration <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )

            if duration > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # Build the final values and recalculate every derived field.
    final_status = requested_status

    if final_status in {"ABSENT", "LEAVE"}:
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            new_punch_in,
            employee["shift_start"],
            employee["shift_end"],
            date,
        )

        if new_punch_out is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = compute_work_hours(
                new_punch_in,
                new_punch_out,
            )
            overtime_minutes = compute_overtime(
                new_punch_out,
                employee["shift_start"],
                employee["shift_end"],
                date,
            )
            half_day = work_hours < 4.50

    # Only include fields that actually changed in the audit trail.
    final_values = {
        "status": final_status,
        "punch_in": (
            to_mongo_utc(new_punch_in)
            if new_punch_in is not None else None
        ),
        "punch_out": (
            to_mongo_utc(new_punch_out)
            if new_punch_out is not None else None
        ),
    }

    changes = {}

    for field, new_value in final_values.items():
        old_value = record.get(field)

        if field in {"punch_in", "punch_out"}:
            old_instant = (
                instant_from_mongo(old_value)
                if old_value is not None else None
            )
            new_instant = (
                instant_from_mongo(new_value)
                if new_value is not None else None
            )
            changed = old_instant != new_instant
            old_for_history = (
                output_datetime(old_instant)
                if old_instant is not None else None
            )
            new_for_history = (
                output_datetime(new_instant)
                if new_instant is not None else None
            )
        else:
            changed = old_value != new_value
            old_for_history = old_value
            new_for_history = new_value

        if changed:
            changes[field] = {
                "from": old_for_history,
                "to": new_for_history,
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The request does not change any attendance fields",
        )

    history_entry = {
        "at": int(datetime.now(UTC).timestamp() * 1000),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    history = record.get("history", [])

    # Atomic update: matching the previous history prevents concurrent
    # regularizations from silently overwriting each other's audit trail.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": history,
        },
        {
            "$set": {
                **final_values,
                "late_minutes": late_minutes,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request",
        )

    return serialize_attendance(updated)




# ---------------------------------------------------------------------------
# Monthly employee analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    # Validate the requested month (YYYY-MM).
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    next_month = (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )
    month_end = next_month - timedelta(days=1)

    # Check that the employee exists.
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    joined_on = date.fromisoformat(employee["joined_on"])

    # Working days are Monday-Friday, starting no earlier than joined_on.
    first_working_date = max(month_start, joined_on)
    working_days = 0

    if first_working_date <= month_end:
        current_day = first_working_date
        while current_day <= month_end:
            if current_day.weekday() < 5:
                working_days += 1
            current_day += timedelta(days=1)

    # Aggregate the employee's records for the requested month.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_attendance_day": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                }
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
                                    {"$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]},
                                    {"$gte": ["$_attendance_day", 2]},
                                    {"$lte": ["$_attendance_day", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
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

    aggregated = list(db.attendance_logs.aggregate(pipeline))

    totals = aggregated[0] if aggregated else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = float(
        Decimal(str(totals["present_days"])).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )

    # Attendance percentage uses four decimal places, half-up.
    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days)) * Decimal("100")
             / Decimal(working_days)).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(totals["leave_days"]),
        "late_count": int(totals["late_count"]),
        "total_late_minutes": int(totals["total_late_minutes"]),
        "total_overtime_minutes": int(totals["total_overtime_minutes"]),
        "attendance_pct": attendance_pct,
    }



@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    try:
        year, month_num = map(int, month.split("-"))
        month_start = date(year, month_num, 1)
        month_end = date(year, month_num, monthrange(year, month_num)[1])
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    next_month = (
        date(year + 1, 1, 1)
        if month_num == 12
        else date(year, month_num + 1, 1)
    )

    # Find employees who had joined by the end of the requested month.
    employee_filter = {
        "joined_on": {"$lte": month_end.isoformat()}
    }
    if department is not None:
        employee_filter["department"] = department

    employees = list(
        db.employees.find(
            employee_filter,
            {"_id": 0, "emp_code": 1, "department": 1},
        )
    )

    if not employees:
        return {"month": month, "items": []}

    employee_codes = [employee["emp_code"] for employee in employees]
    department_by_code = {
        employee["emp_code"]: employee.get("department", "")
        for employee in employees
    }

    start_datetime = datetime(
        year, month_num, 1, tzinfo=timezone.utc
    )
    end_datetime = datetime(
        next_month.year, next_month.month, next_month.day,
        tzinfo=timezone.utc,
    )

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": employee_codes},
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hours": {
                    "$push": {
                        "$cond": [
                            {
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
                            "$work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
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
            }
        },
    ]

    attendance_results = list(db.attendance.aggregate(pipeline))

    department_totals = {}

    # Initialize all departments, including employees with no attendance.
    for employee in employees:
        dept = employee.get("department", "")
        department_totals.setdefault(
            dept,
            {
                "department": dept,
                "headcount": 0,
                "present_days": 0.0,
                "work_hours": [],
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            },
        )
        department_totals[dept]["headcount"] += 1

    for result in attendance_results:
        dept = department_by_code.get(result["_id"])
        if dept is None:
            continue

        totals = department_totals[dept]
        totals["present_days"] += result.get("present_days", 0)
        totals["late_count"] += result.get("late_count", 0)
        totals["total_late_minutes"] += result.get(
            "total_late_minutes", 0
        )
        totals["leave_count"] += result.get("leave_count", 0)
        totals["on_duty_count"] += result.get("on_duty_count", 0)

        totals["work_hours"].extend(
            value
            for value in result.get("work_hours", [])
            if value is not None
        )

    items = []
    for dept in sorted(department_totals):
        totals = department_totals[dept]
        hours = totals["work_hours"]

        avg_work_hours = None
        if hours:
            average = sum(Decimal(str(value)) for value in hours) / len(hours)
            avg_work_hours = float(
                average.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )

        items.append(
            {
                "department": dept,
                "headcount": totals["headcount"],
                "present_days": float(
                    Decimal(str(totals["present_days"])).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                ),
                "avg_work_hours": avg_work_hours,
                "late_count": totals["late_count"],
                "total_late_minutes": totals["total_late_minutes"],
                "leave_count": totals["leave_count"],
                "on_duty_count": totals["on_duty_count"],
            }
        )

    return {"month": month, "items": items}







@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
):
    try:
        year, month_num = map(int, month.split("-"))
        month_start = date(year, month_num, 1)
        next_month = (
            date(year + 1, 1, 1)
            if month_num == 12
            else date(year, month_num + 1, 1)
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    # Get employees that exist, optionally restricting the department.
    employee_filter = {}
    if department is not None:
        employee_filter["department"] = department

    employees = list(
        db.employees.find(
            employee_filter,
            {"_id": 0, "emp_code": 1, "name": 1, "department": 1},
        )
    )

    if not employees:
        return {"month": month, "items": []}

    employee_by_code = {
        employee["emp_code"]: employee for employee in employees
    }
    employee_codes = list(employee_by_code.keys())

    # Sum late minutes for each employee's attendance records in the month.
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": employee_codes},
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
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
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
    ]

    totals = list(db.attendance.aggregate(pipeline))

    # Standard competition ranking: 1, 2, 2, 4.
    items = []
    previous_total = None
    current_rank = 0

    for position, result in enumerate(totals, start=1):
        total = result["total_late_minutes"]

        if total != previous_total:
            current_rank = position
            previous_total = total

        employee = employee_by_code[result["_id"]]

        if current_rank <= limit:
            items.append(
                {
                    "rank": current_rank,
                    "emp_code": result["_id"],
                    "name": employee.get("name", ""),
                    "department": employee.get("department", ""),
                    "total_late_minutes": total,
                    "late_count": result["late_count"],
                }
            )

    return {"month": month, "items": items}



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be >= 'from'",
        )

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    employees = list(
        db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
    )

    if not employees:
        raise HTTPException(status_code=404, detail="Unknown department")

    employee_codes = [e["emp_code"] for e in employees]
    start_text = from_date.isoformat()
    end_text = to_date.isoformat()

    # Aggregate attendance for this department and date range.
    daily_pipeline = [
        {
            "$match": {
                "emp_code": {"$in": employee_codes},
                "date": {"$gte": start_text, "$lte": end_text},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateFromString": {
                        "dateString": "$_id",
                        "format": "%Y-%m-%d",
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"date": 1}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(from_date, datetime.min.time()),
                        datetime.combine(
                            to_date + timedelta(days=1),
                            datetime.min.time(),
                        ),
                    ],
                },
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": [
                                {
                                    "code": e["emp_code"],
                                    "joined": e.get("joined_on"),
                                }
                                for e in employees
                            ],
                            "as": "employee",
                            "cond": {
                                "$lte": [
                                    "$$employee.joined",
                                    {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": "$date",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
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
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.attendance_logs.aggregate(daily_pipeline))

    # If no attendance records exist, $densify has no input to fill.
    # Generate the date range in MongoDB using the employees collection.
    if not items:
        seed_pipeline = [
            {"$limit": 1},
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$literal": datetime.combine(
                            from_date, datetime.min.time()
                        )
                    },
                    "present_count": {"$literal": 0},
                    "late_count": {"$literal": 0},
                }
            },
            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [
                            datetime.combine(from_date, datetime.min.time()),
                            datetime.combine(
                                to_date + timedelta(days=1),
                                datetime.min.time(),
                            ),
                        ],
                    },
                }
            },
            {
                "$set": {
                    "present_count": {"$ifNull": ["$present_count", 0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                    "is_working_day": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                        ]
                    },
                    "headcount": {
                        "$size": {
                            "$filter": {
                                "input": [
                                    {"joined": e.get("joined_on")}
                                    for e in employees
                                ],
                                "as": "employee",
                                "cond": {
                                    "$lte": [
                                        "$$employee.joined",
                                        {
                                            "$dateToString": {
                                                "format": "%Y-%m-%d",
                                                "date": "$date",
                                            }
                                        },
                                    ]
                                },
                            }
                        }
                    },
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            0,
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
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date",
                        }
                    },
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": 1,
                }
            },
            {"$sort": {"date": 1}},
        ]

        items = list(db.employees.aggregate(seed_pipeline))

    def round_half_up(value, places=4):
        if value is None:
            return None
        quantum = Decimal("1").scaleb(-places)
        return float(
            Decimal(str(value)).quantize(
                quantum,
                rounding=ROUND_HALF_UP,
            )
        )

    for item in items:
        item["present_count"] = round_half_up(
            item.get("present_count", 0), 1
        )
        item["attendance_rate"] = round_half_up(
            item.get("attendance_rate"), 4
        )
        item["moving_avg_7d"] = round_half_up(
            item.get("moving_avg_7d"), 4
        )

    return {"department": department, "items": items}





@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str):
    explanations = {
        "health": {
            "method": "GET",
            "path": "/health",
            "purpose": "Checks whether the API is running.",
            "inputs": "No input parameters required.",
        },
        "employees": {
            "method": "GET",
            "path": "/employees",
            "purpose": "Retrieves employee records.",
            "inputs": "Supports the query parameters implemented by the employee listing endpoint.",
        },
        "attendance": {
            "method": "GET",
            "path": "/attendance",
            "purpose": "Retrieves attendance records with optional filters and pagination.",
            "inputs": "emp_code, date_from, date_to, status, page, page_size.",
        },
        "punch-in": {
            "method": "POST",
            "path": "/attendance/punch-in",
            "purpose": "Records an employee's punch-in time and calculates attendance details.",
            "inputs": "The employee code and punch-in timestamp required by the request schema.",
        },
        "punch-out": {
            "method": "POST",
            "path": "/attendance/punch-out",
            "purpose": "Records an employee's punch-out time and calculates work hours and overtime.",
            "inputs": "The employee code and punch-out timestamp required by the request schema.",
        },
        "monthly": {
            "method": "GET",
            "path": "/analytics/employees/{emp_code}/monthly",
            "purpose": "Returns monthly attendance statistics for an employee.",
            "inputs": "emp_code and month in YYYY-MM format.",
        },
        "summary": {
            "method": "GET",
            "path": "/analytics/departments/summary",
            "purpose": "Returns monthly attendance statistics grouped by department.",
            "inputs": "month in YYYY-MM format and an optional department filter.",
        },
        "late": {
            "method": "GET",
            "path": "/analytics/leaderboard/late",
            "purpose": "Ranks employees by total late minutes for a month.",
            "inputs": "month, limit, and an optional department filter.",
        },
        "trend": {
            "method": "GET",
            "path": "/analytics/departments/{department}/trend",
            "purpose": "Returns daily department attendance trends, headcount, attendance rates, and seven-day moving averages.",
            "inputs": "department, from date, and to date in YYYY-MM-DD format.",
        },
        "regularize": {
            "method": "PATCH",
            "path": "/attendance/{emp_code}/{date}",
            "purpose": "Corrects an attendance record and records the changes in its audit history.",
            "inputs": "Employee code, attendance date, correction fields, reason, and regularized_by.",
        },
    }

    key = endpoint.strip().lower().replace("_", "-")

    if key not in explanations:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "Endpoint explanation not found",
                "available_endpoints": sorted(explanations.keys()),
            },
        )

    return {
        "endpoint": key,
        **explanations[key],
    }
