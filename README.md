# Structured Data RAG (Text-to-SQL) Backend

A FastAPI backend for querying tabular data (CSV & Excel) in natural language using PostgreSQL, SQLAlchemy connection pooling, and Groq LLMs.

---

## Architecture Overview

1. **Upload & Schema Ingestion (`/api/upload-data`)**:
   - Accepts CSV or Excel (`.xlsx`, `.xls`) uploads up to `MAX_UPLOAD_MB` (default: 10MB).
   - Dynamically determines schema and maps pandas dtypes to native PostgreSQL types (`BIGINT`, `DOUBLE PRECISION`, `TEXT`, `BOOLEAN`, `TIMESTAMP`).
   - For larger CSVs, streams data in chunks (`chunksize=1000`) and inserts rows in fixed batches (e.g. 500 rows/batch) via parameterized SQLAlchemy statements to guarantee memory bounded execution.
   - Creates an isolated PostgreSQL table prefixed with the session ID (`data_<session_id_no_dashes>_<filename>`).

2. **Text-to-SQL & Safety Layer (`/api/ask-data`)**:
   - Sends **only** the session table name and column names/types to the Groq LLM (never raw row data).
   - Validates generated SQL through a standalone security engine (`app/sql_safety.py`):
     - Strips markdown code blocks.
     - Enforces queries start with `SELECT`.
     - Prohibits destructive/administrative keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `GRANT`, `REVOKE`, `CREATE`, `REPLACE`, `VACUUM`, `COPY`).
     - Rejects stacked queries (`;\s*\S+`).
     - Strictly enforces table isolation (blocks querying system catalogs or other tenant tables).
     - Caps unbounded queries by appending `LIMIT 200`.
   - Executes the validated query with a statement timeout (`STATEMENT_TIMEOUT_MS`).
   - Feeds the returned records back to the Groq LLM to synthesize a natural language response strictly grounded in the returned data.

3. **Session Lifecycle & Data Cleanup (`/api/clear-data` & TTL Eviction)**:
   - Automated TTL cleanup (`SESSION_TTL_SECONDS`) drops expired Postgres tables and frees memory.
   - Explicit cleanup endpoint (`/api/clear-data`) safely drops tables with identifier quoting.

---

## Project Structure

```
.
├── app/
│   ├── __init__.py
│   ├── config.py           # Application settings & environment configuration
│   ├── db.py               # SQLAlchemy engine, connection pooling, batch insert & safe drops
│   ├── llm.py              # Groq API integration for SQL generation & NL synthesis
│   ├── main.py             # FastAPI routes, middleware, and lifecycle handlers
│   ├── schemas.py          # Pydantic request and response models
│   └── sql_safety.py       # Standalone SQL validation, sanitization & safety checks
├── tests/
│   ├── __init__.py
│   ├── test_db_helpers.py  # Unit tests for database utilities & identifier quoting
│   └── test_sql_safety.py  # Unit tests for SQL safety rules & injection prevention
├── .env.example            # Sample environment variables template
├── README.md               # Setup and usage guide
└── requirements.txt        # Python package dependencies
```

---

## Setup & Running Locally

### 1. Prerequisites
- Python 3.10+
- PostgreSQL database instance (local PostgreSQL, Supabase, Neon, RDS, or Railway)
- Groq API Key (from [console.groq.com](https://console.groq.com))

### 2. Install Dependencies

```bash
# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# macOS / Linux:
source venv/bin/activate

# Install required packages
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Copy `.env.example` to `.env` and fill in your connection details:

```bash
cp .env.example .env
```

Edit `.env`:
```ini
DATABASE_URL=postgresql://username:password@localhost:5432/rag_sql_db
GROQ_API_KEY=gsk_your_actual_groq_api_key
GROQ_MODEL=openai/gpt-oss-120b
MAX_UPLOAD_MB=10
CSV_CHUNK_THRESHOLD_MB=2
BATCH_SIZE=500
SESSION_TTL_SECONDS=3600
STATEMENT_TIMEOUT_MS=5000
```

### 4. Run the Backend Application

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive OpenAPI documentation is available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### 5. Run the Frontend Application

```bash
cd frontend
npm install
npm run dev
```

The frontend will start at: `http://localhost:5173/`


---

## API Endpoints Reference

### 1. Health Check
- **Endpoint**: `GET /api/health`
- **Response**:
  ```json
  {
    "status": "healthy",
    "groq_configured": true,
    "database_configured": true
  }
  ```

### 2. Upload Tabular Data (Single or Multi-File)
- **Endpoint**: `POST /api/upload-data`
- **Content-Type**: `multipart/form-data`
- **Form Fields**:
  - `file`: CSV or Excel file (`.xlsx`, `.xls`)
  - `session_id` (optional): Existing session identifier to append another table
- **Response**:
  ```json
  {
    "session_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "tables": [
      {
        "table_name": "data_9b1deb4d3b7d4bad9bdd2b0d7b3dcb6d_sales_data",
        "columns": [
          { "name": "product_name", "type": "TEXT" },
          { "name": "price", "type": "DOUBLE PRECISION" }
        ],
        "row_count": 1500,
        "original_filename": "sales_data.csv"
      }
    ],
    "table_name": "data_9b1deb4d3b7d4bad9bdd2b0d7b3dcb6d_sales_data",
    "columns": [
      { "name": "product_name", "type": "TEXT" },
      { "name": "price", "type": "DOUBLE PRECISION" }
    ],
    "row_count": 1500
  }
  ```

### 3. Ask Natural Language Questions
- **Endpoint**: `POST /api/ask-data`
- **Content-Type**: `application/json`
- **Request Body**:
  ```json
  {
    "session_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "question": "Which customer bought the most units across all sales?",
    "table_name": null
  }
  ```
  *(Note: `table_name` is optional. If omitted with multiple tables, the AI auto-routes or joins across all session tables.)*
- **Response**:
  ```json
  {
    "answer": "The top 3 highest selling products by revenue are Product A ($12,400), Product B ($9,200), and Product C ($8,150).",
    "sql_query": "SELECT product_name, SUM(price * quantity) AS total_revenue FROM \"data_9b1deb4d3b7d4bad9bdd2b0d7b3dcb6d_sales_data\" GROUP BY product_name ORDER BY total_revenue DESC LIMIT 3",
    "row_count": 3,
    "sample_rows": [
      { "product_name": "Product A", "total_revenue": 12400.0 },
      { "product_name": "Product B", "total_revenue": 9200.0 },
      { "product_name": "Product C", "total_revenue": 8150.0 }
    ]
  }
  ```

### 4. Check Data Status
- **Endpoint**: `GET /api/data-status?session_id=<SESSION_ID>`
- **Response**:
  ```json
  {
    "session_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "table_name": "data_9b1deb4d3b7d4bad9bdd2b0d7b3dcb6d_sales_data",
    "columns": [
      { "name": "order_id", "type": "BIGINT" },
      { "name": "product_name", "type": "TEXT" }
    ],
    "row_count": 1500,
    "uploaded_at": "2026-08-18T09:15:00Z"
  }
  ```

### 5. Clear Session Data
- **Endpoint**: `POST /api/clear-data`
- **Content-Type**: `application/x-www-form-urlencoded`
- **Form Fields**:
  - `session_id`: Session ID to drop
- **Response**:
  ```json
  {
    "status": "cleared"
  }
  ```

---

## Example cURL Commands

### Upload Data
```bash
curl -X POST "http://localhost:8000/api/upload-data" \
  -F "file=@sample_sales.csv"
```

### Ask Natural Language Question
```bash
curl -X POST "http://localhost:8000/api/ask-data" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "PASTE_YOUR_SESSION_ID_HERE",
    "question": "Which customer spent the most in total?"
  }'
```

### Get Session Status
```bash
curl -X GET "http://localhost:8000/api/data-status?session_id=PASTE_YOUR_SESSION_ID_HERE"
```

### Clear Session Data
```bash
curl -X POST "http://localhost:8000/api/clear-data" \
  -F "session_id=PASTE_YOUR_SESSION_ID_HERE"
```
