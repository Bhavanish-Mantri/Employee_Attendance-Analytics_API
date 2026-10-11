# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, regularization audit trails, and departmental analytics.

## Running Locally

1. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
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

4. Seed the sample dataset (optional):
   ```bash
   python sample_seed.py
   ```

5. Start the API service:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

## Running Tests

Run the regression test suite:
```bash
python test_app.py
```

## Implementation Highlights

- **All application code is contained in `app/main.py`** in compliance with assessment constraints.
- **Rules R1–R10** fully implemented, including IST timezone conversions, overnight shift assignment, 10-minute grace period boundaries, 30-minute minimum overtime, half-up rounding, half-day calculations, working-day counting for mid-month joiners, and gap-filled trend analysis.
- **Concurrency & Atomicity**: Atomic conditional operations and compound unique indexes guarantee race safety for punch-in, punch-out, and regularization audit history preservation.
- **Performance & Explain**: Idempotent indexes created at startup eliminate collection scans (`COLLSCAN`), verified via `GET /admin/explain/{endpoint}`.
