# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, regularization audit trails, and departmental analytics.

## Architecture & Project Structure

- **All application code is contained in `app/main.py`** in strict compliance with assessment constraints.
- **`app/main.py`**: Complete FastAPI application including data models (Pydantic), business logic helpers (Rules R1–R10), lifespan index initialization, operational endpoints, and MongoDB aggregation pipelines.
- **`requirements.txt`**: Pinned runtime dependencies (`fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`) and test dependencies (`mongomock`).
- **`test_app.py`**: Regression test suite validating all 11 API endpoints, business rules R1–R10, optimistic concurrency, and query plan explanations using `TestClient` with `mongomock`.
- **`REVIEW.md`**: Starter defect audit documenting the 16 resolved issues and corrections.
- **`DECISIONS.md`**: Architectural and engineering decisions covering indexing strategy, race condition handling, tie handling, headcount tracking, and 100x scaling considerations.
- **`sample_seed.py`**: Optional local development utility to seed sample data into a running MongoDB instance.

## Prerequisites

- Python 3.10+ (tested on Python 3.11 / 3.13)
- Optional for live runtime: MongoDB 7.0+ instance (local or Atlas)

## Running Locally

1. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # Linux/macOS:
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Set environment variables (or create a `.env` file):
   ```bash
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

4. Seed the sample dataset (optional local development utility):
   > **Note**: `sample_seed.py` is an optional local helper script that reads documents from local `sample_data/employees.json` and `sample_data/attendance_logs.json` and inserts them into MongoDB. The assessment grader seeds the evaluation database directly.
   ```bash
   python sample_seed.py
   ```

5. Start the API service:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

6. Access the interactive API documentation:
   - Swagger UI: `http://localhost:8000/docs`
   - ReDoc: `http://localhost:8000/redoc`

## Major Endpoints & Analytics

- **Employees**:
  - `POST /employees` — Create employee with unique `emp_code`, email format validation, and shift validation.
  - `GET /employees` — Paginated employee directory with department filtering.
- **Attendance**:
  - `POST /attendance/punch-in` — Punch-in with IST conversion, overnight shift handling (R1), late-grace calculation (R2), and atomic race prevention.
  - `POST /attendance/punch-out` — Punch-out with work hours (R4, R8), minimum overtime threshold (R3), and half-day status computation (R5).
  - `GET /attendance` — Paginated attendance records filtered by date range or department.
  - `PATCH /attendance/{emp_code}/{date}` — Attendance regularization with optimistic concurrency control (`history` array size condition) and immutable audit trail (R6).
- **Analytics (MongoDB Aggregation Pipelines)**:
  - `GET /analytics/monthly/{year}/{month}` — Monthly attendance statistics per employee (working days per R7, attendance percentage per R8).
  - `GET /analytics/department-summary/{year}/{month}` — Department-level aggregation tracking present days, absent days, total overtime, and full active headcount (R9).
  - `GET /analytics/leaderboard` — Standard competition ranking (`1, 2, 2, 4`) of employees with highest late minutes using MongoDB `$rank` window function.
  - `GET /analytics/department-trends` — 30-day moving average and trend analysis with calendar densification / gap filling.
- **Administration & Diagnostics**:
  - `GET /health` — Service health check pinging live MongoDB instance.
  - `GET /admin/explain/{endpoint}` — Returns MongoDB execution plans or pipeline stages for index inspection.

## Running Tests

Run the regression test suite:
```bash
python test_app.py
```
All 11 regression tests run against an in-memory `mongomock` instance with database indexes initialized.

## Performance & Verification Limitations

- **Indexes**: Indexes are created during application startup to support the main employee and attendance queries (`(emp_code)` unique, `(department, emp_code)`, `(emp_code, date)` compound unique, `(emp_code, punch_in)`, `(date, emp_code)`).
- **Verification Status**:
  - Unit and mocked database tests for all 11 endpoints, business rules R1–R10, optimistic concurrency, and admin explain pipelines have been executed and verified via `python test_app.py`.
  - Indexes and aggregation pipelines are implemented to target index-covered sorts and avoid unindexed collection scans. However, query-plan behavior and performance on a live MongoDB instance with 100,000 attendance records have not been benchmarked as part of this submission.
