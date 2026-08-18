from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class ColumnInfo(BaseModel):
    name: str = Field(..., description="Column name in the PostgreSQL table")
    type: str = Field(..., description="PostgreSQL data type for the column")


class TableInfo(BaseModel):
    table_name: str = Field(..., description="Generated PostgreSQL table name")
    columns: List[ColumnInfo] = Field(..., description="List of columns and their PostgreSQL types")
    row_count: int = Field(..., description="Total number of rows loaded in this table")
    original_filename: str = Field(..., description="Original uploaded filename")


class UploadDataResponse(BaseModel):
    session_id: str = Field(..., description="Unique session identifier")
    tables: List[TableInfo] = Field(default_factory=list, description="All tables uploaded in this session")
    # Backward compatibility fields (reflects the latest/active table)
    table_name: str = Field("", description="Latest uploaded PostgreSQL table name")
    columns: List[ColumnInfo] = Field(default_factory=list, description="Latest table columns")
    row_count: int = Field(0, description="Latest table row count")


class AskDataRequest(BaseModel):
    session_id: str = Field(..., description="Session identifier for the uploaded dataset")
    question: str = Field(..., min_length=1, description="Natural language question about the data")
    table_name: Optional[str] = Field(
        None,
        description="Optional specific table name to target. If omitted and multiple tables exist, queries across session tables."
    )


class AskDataResponse(BaseModel):
    answer: str = Field(..., description="Natural language answer grounded in the SQL query results")


class DataStatusResponse(BaseModel):
    session_id: str = Field(..., description="Session identifier")
    tables: List[TableInfo] = Field(default_factory=list, description="List of all tables in the session")
    # Backward compatibility fields
    table_name: str = Field("", description="Primary table name")
    columns: List[ColumnInfo] = Field(default_factory=list, description="Primary table columns")
    row_count: int = Field(0, description="Total row count across session")
    uploaded_at: str = Field(..., description="ISO 8601 timestamp when the session data was uploaded")


class ClearDataResponse(BaseModel):
    status: str = Field("cleared", description="Operation status")


class HealthResponse(BaseModel):
    status: str = Field(..., description="Overall health status ('healthy' or 'degraded')")
    groq_configured: bool = Field(..., description="Whether the Groq API key is present and configured")
    database_configured: bool = Field(..., description="Whether the database connection is active and responsive")
