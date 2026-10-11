# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, attendance regularization, and departmental analytics.

## Architecture & Project Structure

- `app/main.py` — FastAPI application, request validation, business logic, MongoDB operations, startup index initialization, and aggregation pipelines.
- `requirements.txt` — Runtime dependencies and the `mongomock` dependency used by the test suite.
- `test_app.py` — Regression tests for API behavior, business rules, concurrency handling, and query-explanation functionality.
- `REVIEW.md` — Review of the starter implementation's defects and their corresponding corrections.
- `DECISIONS.md` — Engineering decisions covering indexes, concurrency, ranking, headcount tracking, and scalability.

## Prerequisites

- Python 3.10 or later.
- MongoDB 7.0 or later for live database testing and runtime operation.
- Git, if cloning the repository.

## Installation and Setup

### 1. Clone the repository

```bash
git clone https://github.com/Bhavanish-Mantri/Employee_Attendance-Analytics_API.git
cd Employee_Attendance-Analytics_API
```

### 2. Create a virtual environment

**Windows PowerShell:**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

Set the following environment variables before starting the application:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `MONGO_URI` | MongoDB connection URI | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

**Windows PowerShell:**
```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

**Linux/macOS:**
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

> Replace the connection URI with your MongoDB deployment's URI when using MongoDB Atlas or another server.  
> Do not commit database credentials or environment files to GitHub.

### 5. Start the application

Ensure that MongoDB is running and accessible, then execute:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The application initializes its configured database indexes during startup.

### 6. Access the API documentation

Once the application is running:

- Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- ReDoc: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
- Health check: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

The health endpoint checks connectivity to MongoDB. A database connection failure can cause the health check to return an unhealthy response.

---

## API Endpoints

### Health and Employee Management

| Method | Endpoint | Purpose |
| :--- | :--- | :--- |
| `GET` | `/health` | Check application and database connectivity |
| `POST` | `/employees` | Create an employee |
| `GET` | `/employees` | Retrieve a paginated employee directory with supported filters |

Employee creation includes validation such as employee-code uniqueness, email format, and shift-related rules.

### Attendance Management

| Method | Endpoint | Purpose |
| :--- | :--- | :--- |
| `POST` | `/attendance/punch-in` | Record an employee's punch-in |
| `POST` | `/attendance/punch-out` | Record punch-out and calculate attendance metrics |
| `GET` | `/attendance` | Retrieve attendance records with supported filtering and pagination |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize an attendance record |

The attendance implementation covers relevant business rules, including:
- Time-zone conversion and overnight shifts.
- Late-arrival grace periods.
- Work-hour and overtime calculations.
- Half-day status calculation.
- Concurrent attendance updates.
- Regularization history and audit information.

The precise behavior is defined by the implementation in `app/main.py` and documented in `REVIEW.md`.

### Analytics

| Method | Endpoint | Purpose |
| :--- | :--- | :--- |
| `GET` | `/analytics/monthly/{year}/{month}` | Monthly attendance statistics by employee |
| `GET` | `/analytics/department-summary/{year}/{month}` | Department-level attendance and overtime summaries |
| `GET` | `/analytics/leaderboard` | Rank employees by late minutes |
| `GET` | `/analytics/department-trends` | Department attendance trends and moving averages |

The analytics endpoints use MongoDB aggregation pipelines for database-side processing.  
The leaderboard uses competition-style ranking, where tied employees share a rank and the next rank is skipped.

### Administration and Diagnostics

| Method | Endpoint | Purpose |
| :--- | :--- | :--- |
| `GET` | `/admin/explain/{endpoint}` | Inspect supported query execution plans or aggregation pipeline information |

The exact output of the diagnostic endpoint depends on the requested operation and the database capabilities available.

---

## Running the Tests

Install the dependencies first, then run:

```bash
python test_app.py
```

The regression suite uses `mongomock` for mocked database interactions where configured by the tests.

> **Important:** Mocked database tests are not a substitute for integration testing against a live MongoDB server. The test results should be considered verified only after the command has actually been executed successfully in the relevant environment.

---

## MongoDB Indexing and Performance

The application initializes indexes intended to support common employee and attendance queries, including employee-code uniqueness, department-based access, attendance record uniqueness, and date- or punch-in-based queries.

MongoDB aggregation pipelines are used for analytics to perform filtering, grouping, ranking, and trend calculations within the database.

### Verification Limitations

- The presence of index definitions does not, by itself, prove that every query uses an index.
- Actual query execution plans depend on the query, data distribution, indexes, and MongoDB's query planner.
- Live MongoDB integration behavior must be verified against a running database.
- Performance against 100,000 attendance records has not been established unless a benchmark has actually been executed.
- Mocked-database test results do not establish live MongoDB performance or compatibility.

These limitations should be considered when evaluating performance claims.

---

## Sample Data

The repository does not include the sample JSON files referenced by the optional seed utility. Consequently, `sample_seed.py` is not documented as a runnable setup step.

Use an appropriately prepared development database or your own permitted test fixtures when testing locally. Do not use production data or commit database dumps, credentials, or confidential data to the repository.

---

## Engineering Decisions

- See `DECISIONS.md` for the rationale behind the indexing strategy, attendance update concurrency, leaderboard tie handling, department headcount, and scalability considerations.
- See `REVIEW.md` for the review of the original starter defects and their corresponding corrections.

---

## Security and Configuration

- Store connection details in environment variables.
- Never commit `.env` files, passwords, tokens, or private connection strings.
- Use a database account with only the permissions required by the application.
- Use appropriate access controls and network restrictions when deploying the service outside a local development environment.

---

## Project Status

This repository contains the implementation and supporting documentation for the Employee Attendance & Analytics API assignment.

The implementation includes employee and attendance operations, regularization audit history, database indexes, and aggregation-based analytics. Live database integration and large-dataset performance should be evaluated separately from mocked-database testing.
