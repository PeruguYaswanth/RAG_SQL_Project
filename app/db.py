"""
Database management module using SQLAlchemy with connection pooling,
safe identifier quoting, dynamic table generation, and batched execution.
"""

import re
import json
import logging
from typing import List, Dict, Any, Tuple
from contextlib import contextmanager

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text, Engine
from sqlalchemy.sql.elements import quoted_name

from app.config import get_settings

logger = logging.getLogger(__name__)

# Cached SQLAlchemy engine instance
_engine: Engine = None
_engine_url: str = None


def get_engine() -> Engine:
    """
    Creates or returns a singleton SQLAlchemy engine configured with connection pooling.
    Re-creates the engine if DATABASE_URL has changed (e.g., after .env reload).
    """
    global _engine, _engine_url
    settings = get_settings()
    if _engine is None or _engine_url != settings.DATABASE_URL:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        _engine_url = settings.DATABASE_URL
    return _engine


def reset_engine() -> None:
    """Dispose and clear the cached engine (useful in tests or after config changes)."""
    global _engine, _engine_url
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _engine_url = None


def sanitize_identifier(raw_name: str, prefix: str = "col_") -> str:
    """
    Sanitize an identifier (table or column name) to be safe for PostgreSQL:
    - Lowercase
    - Replace spaces and special characters with underscores
    - Remove consecutive underscores
    - Ensure it does not start with a digit or special character
    """
    # Replace non-alphanumeric characters with underscores
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", str(raw_name).strip().lower())
    # Collapse multiple underscores
    clean = re.sub(r"_+", "_", clean).strip("_")
    # If empty or starts with a digit, prefix it
    if not clean:
        clean = f"{prefix}empty"
    elif clean[0].isdigit():
        clean = f"{prefix}{clean}"
    # Truncate to PostgreSQL's 63-character identifier limit
    return clean[:63]


def quote_ident(ident: str) -> str:
    """
    Safely quote an identifier for PostgreSQL using double quotes and escaping inner quotes.
    """
    sanitized = ident.replace('"', '""')
    return f'"{sanitized}"'


def map_pandas_dtype_to_postgres(dtype: Any) -> str:
    """
    Map a pandas series dtype to a PostgreSQL column data type.
    """
    dtype_str = str(dtype).lower()
    if "int" in dtype_str:
        return "BIGINT"
    elif "float" in dtype_str or "double" in dtype_str:
        return "DOUBLE PRECISION"
    elif "bool" in dtype_str:
        return "BOOLEAN"
    elif "datetime" in dtype_str or "timestamp" in dtype_str:
        return "TIMESTAMP"
    else:
        return "TEXT"


def create_table_from_dataframe_schema(
    engine: Engine,
    table_name: str,
    df: pd.DataFrame
) -> List[Dict[str, str]]:
    """
    Creates a PostgreSQL table dynamically based on DataFrame column types.
    Returns a list of dictionaries with column metadata: [{'name': ..., 'type': ...}].
    """
    quoted_table = quote_ident(table_name)
    column_definitions = []
    schema_info = []

    # Map column names and types
    for col in df.columns:
        col_name = str(col)
        pg_type = map_pandas_dtype_to_postgres(df[col].dtype)
        quoted_col = quote_ident(col_name)
        column_definitions.append(f"{quoted_col} {pg_type}")
        schema_info.append({"name": col_name, "type": pg_type})

    create_sql = f"CREATE TABLE {quoted_table} (\n  " + ",\n  ".join(column_definitions) + "\n);"

    with engine.begin() as conn:
        # Drop table if exists to ensure clean state
        conn.execute(text(f"DROP TABLE IF EXISTS {quoted_table} CASCADE;"))
        conn.execute(text(create_sql))

    logger.info(f"Created table {table_name} with {len(schema_info)} columns.")
    return schema_info


def _coerce_value(val: Any) -> Any:
    """
    Coerce a Python/NumPy/Pandas value into a type psycopg2 can natively adapt.

    psycopg2 handles: None, bool, int, float, str, datetime, date, bytes.
    It CANNOT handle: dict, list, numpy scalars, pandas Timestamp, numpy bool_.

    Rules applied in order:
    1. None  → pass through as SQL NULL
    2. pandas Timestamp / datetime-like  → Python datetime
    3. numpy integer/float/bool scalars  → Python int/float/bool via .item()
    4. dict or list  → JSON string (TEXT column)
    5. Any remaining non-primitive  → str()
    """
    if val is None:
        return None

    # pandas Timestamp, datetime.datetime, datetime.date
    if hasattr(val, "to_pydatetime"):
        return val.to_pydatetime()

    # numpy scalars (np.int64, np.float32, np.bool_, etc.)
    if isinstance(val, np.generic):
        return val.item()

    # Python dict or list → serialize to JSON string so it can be stored as TEXT
    if isinstance(val, (dict, list)):
        try:
            return json.dumps(val, ensure_ascii=False, default=str)
        except Exception:
            return str(val)

    # Any other exotic type → stringify
    if not isinstance(val, (bool, int, float, str, bytes)):
        return str(val)

    return val


def insert_dataframe_in_batches(
    engine: Engine,
    table_name: str,
    df: pd.DataFrame,
    batch_size: int = 500
) -> int:
    """
    Inserts rows from a DataFrame into PostgreSQL in fixed-size batches using
    parameterized multi-row inserts to guarantee bounded memory and query size.
    """
    if df.empty:
        return 0

    quoted_table = quote_ident(table_name)
    columns = list(df.columns)
    quoted_cols = ", ".join([quote_ident(col) for col in columns])
    param_names = [f"val_{i}" for i in range(len(columns))]
    param_placeholders = ", ".join([f":{p}" for p in param_names])

    insert_stmt = text(f"INSERT INTO {quoted_table} ({quoted_cols}) VALUES ({param_placeholders});")

    total_rows = len(df)
    inserted_count = 0

    # Replace pandas NaN/NaT with None so psycopg2 converts them to SQL NULL
    clean_df = df.where(pd.notnull(df), None)

    with engine.begin() as conn:
        for start_idx in range(0, total_rows, batch_size):
            end_idx = min(start_idx + batch_size, total_rows)
            chunk = clean_df.iloc[start_idx:end_idx]

            # Prepare batch parameter dictionaries
            batch_params = []
            for _, row in chunk.iterrows():
                row_dict = {}
                for col_idx, col in enumerate(columns):
                    val = row[col]
                    row_dict[f"val_{col_idx}"] = _coerce_value(val)
                batch_params.append(row_dict)

            conn.execute(insert_stmt, batch_params)
            inserted_count += len(batch_params)

    logger.info(f"Inserted {inserted_count} rows into {table_name} in batches of {batch_size}.")
    return inserted_count


def execute_safe_query(
    engine: Engine,
    query_sql: str,
    timeout_ms: int = 5000
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Executes a validated SELECT query against PostgreSQL with an enforced statement timeout.
    Returns (column_names, rows_as_dicts).
    """
    with engine.connect() as conn:
        # Enforce execution statement timeout at session/connection level
        conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)};"))
        result = conn.execute(text(query_sql))
        columns = list(result.keys())
        rows = [dict(row._mapping) for row in result]
        return columns, rows


def drop_table_safely(engine: Engine, table_name: str) -> None:
    """
    Drops a table using safely quoted identifiers (blocks SQL injection in table name).
    """
    quoted_table = quote_ident(table_name)
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {quoted_table} CASCADE;"))
    logger.info(f"Dropped table {table_name} successfully.")


def check_db_health(engine: Engine) -> bool:
    """
    Tests if the database connection is active and responsive.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        return True
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False
