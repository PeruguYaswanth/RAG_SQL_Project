"""
Main FastAPI Application module for Text-to-SQL RAG on structured data.
Supports multi-table sessions and universal data/database formats:
SQLite (.db, .sqlite, .sqlite3), SQL scripts (.sql), Parquet (.parquet),
JSON/JSONL (.json, .jsonl, .ndjson), TSV (.tsv, .tab, .txt), CSV (.csv), and Excel (.xlsx, .xls).
"""

import io
import os
import json
import uuid
import sqlite3
import tempfile
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import (
    get_engine,
    sanitize_identifier,
    create_table_from_dataframe_schema,
    insert_dataframe_in_batches,
    execute_safe_query,
    drop_table_safely,
    check_db_health,
)
from app.sql_safety import validate_and_sanitize_sql
from app.llm import generate_sql_query, generate_natural_language_answer
from app.schemas import (
    UploadDataResponse,
    AskDataRequest,
    AskDataResponse,
    DataStatusResponse,
    ClearDataResponse,
    HealthResponse,
    ColumnInfo,
    TableInfo,
)

# Configure logging format and levels
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("rag_sql_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context: Logs startup diagnostics, verifies DB and Groq configuration,
    and performs graceful cleanup on shutdown.
    """
    logger.info("Initializing Structured Data RAG API...")
    settings = get_settings()

    # Verify PostgreSQL on startup
    try:
        engine = get_engine()
        if check_db_health(engine):
            logger.info("PostgreSQL database connection established successfully.")
        else:
            logger.warning("PostgreSQL database connection check failed during startup.")
    except Exception as e:
        logger.error(f"PostgreSQL connection error during startup: {e}")

    # Verify Groq key configuration on startup
    if settings.GROQ_API_KEY and settings.GROQ_API_KEY.strip():
        logger.info(f"Groq API configured with model: '{settings.GROQ_MODEL}'")
    else:
        logger.warning("GROQ_API_KEY is not configured in .env.")

    yield
    logger.info("Structured Data RAG API is shutting down...")


# Initialize FastAPI application with lifespan management
app = FastAPI(
    title="Structured Data RAG API (Text-to-SQL)",
    description="Backend API for querying structured database and tabular files using PostgreSQL, SQLAlchemy, and Groq LLM.",
    version="1.2.0",
    lifespan=lifespan
)

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:3000",
    ],
    allow_origin_regex=r"^https:\/\/.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store supporting multiple tables per session
# Key: session_id (str)
sessions: Dict[str, Dict[str, Any]] = {}

SUPPORTED_EXTENSIONS = [
    ".csv", ".tsv", ".tab", ".txt",
    ".xlsx", ".xls",
    ".db", ".sqlite", ".sqlite3",
    ".sql",
    ".parquet",
    ".json", ".jsonl", ".ndjson",
]


def cleanup_expired_sessions() -> None:
    """
    Evicts expired sessions from memory and drops all of their associated PostgreSQL tables
    if the session age exceeds SESSION_TTL_SECONDS.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expired_ids = []

    for session_id, session_data in list(sessions.items()):
        uploaded_at = session_data.get("uploaded_at")
        if uploaded_at:
            age_seconds = (now - uploaded_at).total_seconds()
            if age_seconds > settings.SESSION_TTL_SECONDS:
                expired_ids.append(session_id)

    if expired_ids:
        engine = get_engine()
        for s_id in expired_ids:
            session_data = sessions.get(s_id, {})
            for tbl in session_data.get("tables", []):
                table_name = tbl.get("table_name")
                if table_name:
                    try:
                        drop_table_safely(engine, table_name)
                        logger.info(f"Cleaned up expired table '{table_name}' for session '{s_id}'.")
                    except Exception as e:
                        logger.error(f"Error dropping expired table '{table_name}': {e}")
            sessions.pop(s_id, None)


@app.middleware("http")
async def session_cleanup_middleware(request: Request, call_next):
    """
    Runs session cleanup on incoming requests to ensure expired tables/sessions are evicted.
    """
    try:
        cleanup_expired_sessions()
    except Exception as e:
        logger.warning(f"Session cleanup encountered an issue: {e}")
    response = await call_next(request)
    return response


@app.get("/health", tags=["Health"])
async def root_health():
    """
    Root health check endpoint: Verifies application runtime status,
    Groq API key configuration, and PostgreSQL database connectivity.
    """
    settings = get_settings()
    groq_configured = bool(settings.GROQ_API_KEY and settings.GROQ_API_KEY.strip())
    database_configured = False

    try:
        engine = get_engine()
        database_configured = check_db_health(engine)
    except Exception as e:
        logger.error(f"Database health check error: {e}")

    overall_status = "healthy" if (groq_configured and database_configured) else "degraded"
    status_code = status.HTTP_200_OK if overall_status == "healthy" else status.HTTP_200_OK
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall_status,
            "app": "running",
            "groq_configured": groq_configured,
            "database_configured": database_configured
        }
    )


@app.get("/health/db", tags=["Health"])
async def db_health():
    """
    Dedicated database health check: Verifies PostgreSQL connectivity.
    Returns HTTP 200 if connected, or HTTP 503 Service Unavailable if database is unreachable.
    """
    database_configured = False
    error_detail = None
    try:
        engine = get_engine()
        database_configured = check_db_health(engine)
    except Exception as e:
        error_detail = str(e)
        logger.error(f"Database health check failed: {e}")

    if database_configured:
        return {"status": "healthy", "database": "connected"}
    else:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "detail": error_detail or "Failed to connect to PostgreSQL database."
            }
        )


@app.get("/api/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """
    Frontend API health check endpoint: Verifies application status, Groq API key configuration,
    and PostgreSQL database connectivity.
    """
    settings = get_settings()
    groq_configured = bool(settings.GROQ_API_KEY and settings.GROQ_API_KEY.strip())
    database_configured = False

    try:
        engine = get_engine()
        database_configured = check_db_health(engine)
    except Exception as e:
        logger.error(f"Database health check error: {e}")

    overall_status = "healthy" if (groq_configured and database_configured) else "degraded"
    return HealthResponse(
        status=overall_status,
        groq_configured=groq_configured,
        database_configured=database_configured
    )


def sanitize_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure every column in the DataFrame contains only psycopg2-insertable scalars.

    Columns whose values include Python dicts or lists (common in JSON/Parquet with
    nested data) would cause a 'can't adapt type dict' error in psycopg2.
    This function converts any such column to its JSON-string representation so it
    can be stored as a PostgreSQL TEXT column without crashing.
    """
    for col in df.columns:
        # Check a sample of non-null values to detect nested types
        sample = df[col].dropna()
        if len(sample) > 0 and isinstance(sample.iloc[0], (dict, list)):
            df[col] = df[col].apply(
                lambda v: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
            )
    return df


def extract_dataframes_from_file(filename: str, content: bytes, ext: str) -> List[Tuple[str, pd.DataFrame]]:
    """
    Parses various database and data file formats (SQLite, SQL dumps, Parquet, JSON, TSV, CSV, Excel)
    and returns a list of (table_label, dataframe) pairs.
    """
    base_name = os.path.splitext(filename)[0]
    results: List[Tuple[str, pd.DataFrame]] = []

    # 1. SQLite Database Files (.db, .sqlite, .sqlite3)
    if ext in [".db", ".sqlite", ".sqlite3"]:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name

        try:
            conn = sqlite3.connect(tmp_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            sqlite_tables = [r[0] for r in cursor.fetchall()]
            if not sqlite_tables:
                raise HTTPException(status_code=400, detail="The uploaded SQLite database contains no user tables.")

            for tbl_name in sqlite_tables:
                df = pd.read_sql_query(f'SELECT * FROM "{tbl_name}"', conn)
                if not df.empty and len(df) > 0:
                    results.append((f"{base_name}_{tbl_name}", df))
            conn.close()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        if not results:
            raise HTTPException(status_code=400, detail="All tables in the SQLite database are empty (0 rows).")
        return results

    # 2. SQL Dump / Script Files (.sql)
    elif ext == ".sql":
        sql_script = content.decode("utf-8", errors="replace")
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(sql_script)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
            sql_tables = [r[0] for r in cursor.fetchall()]
            if not sql_tables:
                raise HTTPException(
                    status_code=400,
                    detail="The SQL script did not create any readable tables with data."
                )

            for tbl_name in sql_tables:
                df = pd.read_sql_query(f'SELECT * FROM "{tbl_name}"', conn)
                if not df.empty and len(df) > 0:
                    results.append((f"{base_name}_{tbl_name}", df))
            conn.close()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to execute SQL dump file: {str(e)}")

        if not results:
            raise HTTPException(status_code=400, detail="No data rows found in tables created by SQL script.")
        return results

    # 3. Parquet Columnar Database Files (.parquet)
    elif ext == ".parquet":
        try:
            df = pd.read_parquet(io.BytesIO(content))
            if df.empty or len(df) == 0:
                raise HTTPException(status_code=400, detail="Uploaded Parquet file contains 0 data rows.")
            results.append((base_name, df))
            return results
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse Parquet file: {str(e)}")

    # 4. JSON / JSON Lines (.json, .jsonl, .ndjson)
    elif ext in [".json", ".jsonl", ".ndjson"]:
        try:
            raw_text = content.decode("utf-8", errors="replace")
            raw_data = json.loads(raw_text)

            if ext in [".jsonl", ".ndjson"] or (isinstance(raw_data, list) and len(raw_data) > 0 and not isinstance(raw_data[0], (dict, list))):
                # Newline-delimited: each line is a JSON record
                df = pd.read_json(io.BytesIO(content), lines=(ext in [".jsonl", ".ndjson"]))
            elif isinstance(raw_data, list):
                # Standard array of record objects — normalize flattens nested keys
                df = pd.json_normalize(raw_data)
            elif isinstance(raw_data, dict):
                # Find the first list value (e.g. {"results": [...], "total": 5} pattern)
                first_list = next((v for v in raw_data.values() if isinstance(v, list)), None)
                if first_list:
                    df = pd.json_normalize(first_list)
                else:
                    # Single-record object
                    df = pd.json_normalize([raw_data])
            else:
                raise ValueError("JSON must contain an array of records or an object.")

            if df.empty or len(df) == 0:
                raise HTTPException(status_code=400, detail="Uploaded JSON file contains 0 data rows.")
            results.append((base_name, sanitize_dataframe_columns(df)))
            return results
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse JSON file: {str(e)}")

    # 5. TSV & Delimited Text Files (.tsv, .tab, .txt)
    elif ext in [".tsv", ".tab", ".txt"]:
        try:
            try:
                df = pd.read_csv(io.BytesIO(content), sep=None, engine="python")
            except Exception:
                df = pd.read_csv(io.BytesIO(content), sep="\t")
            if df.empty or len(df) == 0:
                raise HTTPException(status_code=400, detail="Uploaded text/TSV file contains 0 data rows.")
            results.append((base_name, df))
            return results
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse TSV/delimited file: {str(e)}")

    # 6. Excel Spreadsheets (.xlsx, .xls)
    elif ext in [".xlsx", ".xls"]:
        try:
            excel_sheets = pd.read_excel(io.BytesIO(content), sheet_name=None)
            for sheet_name, sheet_df in excel_sheets.items():
                if not sheet_df.empty and len(sheet_df) > 0:
                    label = f"{base_name}_{sheet_name}" if len(excel_sheets) > 1 else base_name
                    results.append((label, sheet_df))

            if not results:
                raise HTTPException(status_code=400, detail="Uploaded Excel file contains 0 data rows.")
            return results
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse Excel file: {str(e)}")

    # 7. Standard CSV (.csv)
    elif ext == ".csv":
        try:
            df = pd.read_csv(io.BytesIO(content))
            if df.empty or len(df) == 0:
                raise HTTPException(status_code=400, detail="Uploaded CSV file contains 0 data rows.")
            results.append((base_name, df))
            return results
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse CSV file: {str(e)}")

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file format '{ext}'. Supported formats include: "
                "SQLite (.db, .sqlite, .sqlite3), SQL scripts (.sql), Parquet (.parquet), "
                "JSON (.json, .jsonl), TSV (.tsv, .tab, .txt), CSV (.csv), and Excel (.xlsx, .xls)."
            )
        )


@app.post("/api/upload-data", response_model=UploadDataResponse, tags=["Data Management"])
async def upload_data(
    file: UploadFile = File(..., description="Database or tabular file to upload (.db, .sqlite, .sql, .parquet, .json, .csv, .xlsx, etc.)"),
    session_id: Optional[str] = Form(None, description="Optional existing session ID to append table(s) to")
):
    """
    Uploads a dataset or database file, parses tables safely into memory,
    dynamically creates dedicated PostgreSQL table(s) with inferred types,
    and loads the data in batches.
    
    Supports SQLite database files (.db, .sqlite), SQL dumps (.sql), Parquet (.parquet),
    JSON/JSONL (.json, .jsonl), TSV (.tsv), CSV (.csv), and Excel (.xlsx, .xls).
    """
    cleanup_expired_sessions()
    settings = get_settings()

    # Session ID validation or generation
    if session_id:
        if session_id not in sessions:
            raise HTTPException(status_code=404, detail="Session not found or expired.")
        active_session_id = session_id
    else:
        active_session_id = str(uuid.uuid4())

    # Validate file extension
    filename = file.filename or "data.csv"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file format '{ext}'. Supported formats: "
                "SQLite (.db, .sqlite, .sqlite3), SQL (.sql), Parquet (.parquet), "
                "JSON (.json, .jsonl), TSV (.tsv, .tab, .txt), CSV (.csv), and Excel (.xlsx, .xls)."
            )
        )

    # Read and validate file content & size
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty (0 bytes).")
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File size ({len(content) / (1024 * 1024):.2f} MB) exceeds limit of {settings.MAX_UPLOAD_MB} MB."
        )

    # Extract all DataFrames from the uploaded file
    extracted_tables = extract_dataframes_from_file(filename, content, ext)

    engine = get_engine()
    session_no_dashes = active_session_id.replace("-", "")
    new_table_entries = []

    # Get existing table names in the session to avoid naming collisions
    existing_tables = [t["table_name"] for t in sessions.get(active_session_id, {}).get("tables", [])]

    for label, df in extracted_tables:
        # Sanitize columns and cell values
        df = sanitize_dataframe_columns(df)
        df.columns = [sanitize_identifier(col, prefix="col_") for col in df.columns]

        # Generate unique isolated table name
        sanitized_label = sanitize_identifier(label, prefix="dataset_")
        base_table_name = f"data_{session_no_dashes}_{sanitized_label}"[:55]

        table_name = base_table_name
        counter = 2
        while table_name in existing_tables or any(t["table_name"] == table_name for t in new_table_entries):
            table_name = f"{base_table_name}_{counter}"[:63]
            counter += 1

        # Create PostgreSQL table and load records in batches
        try:
            columns_info = create_table_from_dataframe_schema(engine, table_name, df)
            total_rows = insert_dataframe_in_batches(engine, table_name, df, batch_size=settings.BATCH_SIZE)
        except Exception as e:
            logger.error(f"Failed to create/insert table '{table_name}' in PostgreSQL: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Database import failed: {str(e)}"
            )

        table_entry = {
            "table_name": table_name,
            "columns": columns_info,
            "row_count": total_rows,
            "original_filename": f"{filename} ({label})" if len(extracted_tables) > 1 else filename,
        }
        new_table_entries.append(table_entry)

    # Append to existing session or initialize new multi-table session
    if active_session_id in sessions:
        sessions[active_session_id]["tables"].extend(new_table_entries)
        sessions[active_session_id]["uploaded_at"] = datetime.now(timezone.utc)
    else:
        sessions[active_session_id] = {
            "session_id": active_session_id,
            "tables": new_table_entries,
            "uploaded_at": datetime.now(timezone.utc),
        }

    session_tables = sessions[active_session_id]["tables"]
    primary_new_table = new_table_entries[0]

    return UploadDataResponse(
        session_id=active_session_id,
        tables=[TableInfo(**t) for t in session_tables],
        table_name=primary_new_table["table_name"],
        columns=[ColumnInfo(**c) for c in primary_new_table["columns"]],
        row_count=primary_new_table["row_count"],
    )


@app.post("/api/ask-data", response_model=AskDataResponse, tags=["Query & RAG"])
async def ask_data(request: AskDataRequest):
    """
    Accepts a natural language question about an active session's dataset(s).
    
    Multi-table routing logic:
    - If table_name is specified: restricts query generation and SQL safety validation to that single table.
    - If table_name is omitted and session has 1 table: queries that single table.
    - If table_name is omitted and session has multiple tables: passes all table schemas to LLM,
      permitting queries / JOINs across session tables while enforcing table isolation.
    """
    cleanup_expired_sessions()
    settings = get_settings()

    # Session existence check
    session = sessions.get(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    session_tables = session.get("tables", [])
    if not session_tables:
        raise HTTPException(status_code=400, detail="Session contains no active tables.")

    # Determine target tables and allowed tables for safety validation
    if request.table_name:
        matching_tables = [t for t in session_tables if t["table_name"].lower() == request.table_name.lower()]
        if not matching_tables:
            raise HTTPException(
                status_code=400,
                detail=f"Table '{request.table_name}' does not exist in session."
            )
        target_tables = matching_tables
        allowed_tables = [matching_tables[0]["table_name"]]
    else:
        target_tables = session_tables
        allowed_tables = [t["table_name"] for t in session_tables]

    # 1. Generate SQL from schema and question using LLM
    try:
        raw_sql = generate_sql_query(
            tables=target_tables,
            question=request.question
        )
        logger.info(f"Generated SQL for session '{request.session_id}': {raw_sql}")
    except Exception as e:
        logger.error(f"Error during SQL generation from LLM: {e}")
        return AskDataResponse(
            answer="I couldn't generate a valid query for that question."
        )

    # 2. Critical Multi-Table Safety Validation
    is_safe, sanitized_sql, error_reason = validate_and_sanitize_sql(
        raw_query=raw_sql,
        allowed_tables=allowed_tables
    )
    if not is_safe:
        logger.warning(
            f"Blocked unsafe SQL query for session '{request.session_id}'. "
            f"Reason: {error_reason} | Query: {raw_sql}"
        )
        return AskDataResponse(
            answer="I couldn't generate a valid query for that question."
        )

    # 3. Safe Query Execution with Timeout Protection
    engine = get_engine()
    try:
        _, rows = execute_safe_query(
            engine=engine,
            query_sql=sanitized_sql,
            timeout_ms=settings.STATEMENT_TIMEOUT_MS
        )
    except Exception as e:
        logger.warning(f"Database query execution error for session '{request.session_id}': {e}")
        return AskDataResponse(
            answer="I couldn't generate a valid query for that question."
        )

    # 4. Generate Natural Language Answer Grounded in Results
    try:
        nl_answer = generate_natural_language_answer(
            question=request.question,
            sql_query=sanitized_sql,
            rows=rows,
            row_count=len(rows)
        )
    except Exception as e:
        logger.error(f"Error generating natural language answer from LLM: {e}")
        nl_answer = f"The query executed successfully and returned {len(rows)} record(s)."

    return AskDataResponse(
        answer=nl_answer
    )


@app.get("/api/data-status", response_model=DataStatusResponse, tags=["Data Management"])
async def data_status(session_id: str = Query(..., description="Active session ID")):
    """
    Returns metadata (all tables, columns, total row counts, upload timestamp) for an active session.
    """
    cleanup_expired_sessions()
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    session_tables = session.get("tables", [])
    primary_table = session_tables[0] if session_tables else {"table_name": "", "columns": [], "row_count": 0}
    total_rows = sum(t["row_count"] for t in session_tables)

    return DataStatusResponse(
        session_id=session_id,
        tables=[TableInfo(**t) for t in session_tables],
        table_name=primary_table["table_name"],
        columns=[ColumnInfo(**c) for c in primary_table["columns"]],
        row_count=total_rows,
        uploaded_at=session["uploaded_at"].isoformat(),
    )


@app.post("/api/clear-data", response_model=ClearDataResponse, tags=["Data Management"])
async def clear_data(
    session_id: str = Form(..., description="Session ID of the dataset to drop and clear")
):
    """
    Drops ALL of the session's PostgreSQL tables using safely quoted identifiers
    and removes session state from memory.
    """
    cleanup_expired_sessions()
    session = sessions.get(session_id)
    if not session:
        return ClearDataResponse(status="cleared")

    engine = get_engine()
    for tbl in session.get("tables", []):
        table_name = tbl.get("table_name")
        if table_name:
            try:
                drop_table_safely(engine, table_name)
            except Exception as e:
                logger.error(f"Error dropping table '{table_name}': {e}")

    sessions.pop(session_id, None)
    return ClearDataResponse(status="cleared")
