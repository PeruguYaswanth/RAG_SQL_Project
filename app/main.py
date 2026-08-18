"""
Main FastAPI Application module for Text-to-SQL RAG on structured data.
Supports single and multiple uploaded tables per session.
"""

import io
import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Request
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

# Initialize FastAPI application
app = FastAPI(
    title="Structured Data RAG API (Text-to-SQL)",
    description="Backend API for querying tabular datasets using PostgreSQL, SQLAlchemy, and Groq LLM.",
    version="1.1.0"
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
# Value: {
#   "session_id": str,
#   "tables": [
#     {
#       "table_name": str,
#       "columns": List[Dict[str, str]],
#       "row_count": int,
#       "original_filename": str
#     }, ...
#   ],
#   "uploaded_at": datetime
# }
sessions: Dict[str, Dict[str, Any]] = {}


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


@app.get("/api/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """
    Health check endpoint: Verifies application status, Groq API key configuration,
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


@app.post("/api/upload-data", response_model=UploadDataResponse, tags=["Data Management"])
async def upload_data(
    file: UploadFile = File(..., description="CSV or Excel (.xlsx) file to upload"),
    session_id: Optional[str] = Form(None, description="Optional existing session ID to append table to")
):
    """
    Uploads a CSV or Excel dataset, parses it safely with bounded memory usage,
    dynamically generates a dedicated PostgreSQL table with inferred types,
    and loads the data in batches.
    
    If called with an existing session_id, appends the newly uploaded table to the session's tables list.
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
    if ext not in [".csv", ".xlsx", ".xls"]:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format '{ext}'. Only CSV (.csv) and Excel (.xlsx, .xls) files are supported."
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

    # Generate sanitized and isolated table name
    sanitized_fn = sanitize_identifier(os.path.splitext(filename)[0], prefix="dataset_")
    session_no_dashes = active_session_id.replace("-", "")
    base_table_name = f"data_{session_no_dashes}_{sanitized_fn}"[:55]

    # Guarantee table name uniqueness within session if uploading duplicate filenames
    existing_tables = [t["table_name"] for t in sessions.get(active_session_id, {}).get("tables", [])]
    table_name = base_table_name
    counter = 2
    while table_name in existing_tables:
        table_name = f"{base_table_name}_{counter}"[:63]
        counter += 1

    engine = get_engine()
    chunk_threshold_bytes = settings.CSV_CHUNK_THRESHOLD_MB * 1024 * 1024
    columns_info = []
    total_row_count = 0

    try:
        if ext == ".csv":
            # For larger CSVs, process in chunks to keep memory usage strictly bounded
            if len(content) > chunk_threshold_bytes:
                csv_file_buffer = io.BytesIO(content)
                reader = pd.read_csv(csv_file_buffer, chunksize=1000)
                is_first_chunk = True

                for chunk_df in reader:
                    if chunk_df.empty:
                        continue
                    # Sanitize column names
                    chunk_df.columns = [sanitize_identifier(col, prefix="col_") for col in chunk_df.columns]

                    if is_first_chunk:
                        columns_info = create_table_from_dataframe_schema(engine, table_name, chunk_df)
                        is_first_chunk = False

                    inserted = insert_dataframe_in_batches(
                        engine, table_name, chunk_df, batch_size=settings.BATCH_SIZE
                    )
                    total_row_count += inserted

                if is_first_chunk or total_row_count == 0:
                    raise HTTPException(status_code=400, detail="Uploaded CSV file contains 0 data rows.")
            else:
                # Small CSV file
                df = pd.read_csv(io.BytesIO(content))
                if df.empty or len(df) == 0:
                    raise HTTPException(status_code=400, detail="Uploaded CSV file contains 0 data rows.")
                df.columns = [sanitize_identifier(col, prefix="col_") for col in df.columns]
                columns_info = create_table_from_dataframe_schema(engine, table_name, df)
                total_row_count = insert_dataframe_in_batches(
                    engine, table_name, df, batch_size=settings.BATCH_SIZE
                )
        else:
            # Excel file (.xlsx, .xls)
            df = pd.read_excel(io.BytesIO(content))
            if df.empty or len(df) == 0:
                raise HTTPException(status_code=400, detail="Uploaded Excel file contains 0 data rows.")
            df.columns = [sanitize_identifier(col, prefix="col_") for col in df.columns]
            columns_info = create_table_from_dataframe_schema(engine, table_name, df)
            total_row_count = insert_dataframe_in_batches(
                engine, table_name, df, batch_size=settings.BATCH_SIZE
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to parse or load uploaded file: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to parse data file: {str(e)}")

    new_table_entry = {
        "table_name": table_name,
        "columns": columns_info,
        "row_count": total_row_count,
        "original_filename": filename,
    }

    # Append to existing session or initialize new multi-table session
    if active_session_id in sessions:
        sessions[active_session_id]["tables"].append(new_table_entry)
        sessions[active_session_id]["uploaded_at"] = datetime.now(timezone.utc)
    else:
        sessions[active_session_id] = {
            "session_id": active_session_id,
            "tables": [new_table_entry],
            "uploaded_at": datetime.now(timezone.utc),
        }

    session_tables = sessions[active_session_id]["tables"]

    return UploadDataResponse(
        session_id=active_session_id,
        tables=[TableInfo(**t) for t in session_tables],
        table_name=table_name,
        columns=[ColumnInfo(**c) for c in columns_info],
        row_count=total_row_count,
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
        # If already expired or cleared, return success idempotently
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
