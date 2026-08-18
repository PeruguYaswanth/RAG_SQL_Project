"""
LLM integration module using Groq API for Text-to-SQL conversion and
grounded natural language answer generation across single and multiple tables.
"""

import json
import logging
from typing import List, Dict, Any, Optional, Union

from groq import Groq
from app.config import get_settings

logger = logging.getLogger(__name__)


def get_groq_client() -> Optional[Groq]:
    """
    Returns an initialized Groq client if GROQ_API_KEY is configured.
    """
    settings = get_settings()
    if not settings.GROQ_API_KEY:
        return None
    return Groq(api_key=settings.GROQ_API_KEY)


def format_table_schema_prompt(table_info: Dict[str, Any]) -> str:
    """
    Formats a single table's schema and columns for prompt inclusion.
    """
    table_name = table_info["table_name"]
    orig_fn = table_info.get("original_filename")
    fn_header = f" (from file: '{orig_fn}')" if orig_fn else ""
    
    col_lines = []
    for col in table_info.get("columns", []):
        col_name = col["name"] if isinstance(col, dict) else col.name
        col_type = col["type"] if isinstance(col, dict) else col.type
        col_lines.append(f'    - "{col_name}" ({col_type})')
    
    return f'  Table: "{table_name}"{fn_header}\n  Columns:\n' + "\n".join(col_lines)


def generate_sql_query(
    tables: Union[List[Dict[str, Any]], str],
    columns: Optional[List[Dict[str, str]]] = None,
    question: str = ""
) -> str:
    """
    Generates a single PostgreSQL SELECT query from natural language question
    based on the provided table schema(s). Supports both single table and multiple tables.
    """
    settings = get_settings()
    client = get_groq_client()
    if not client:
        raise ValueError("GROQ_API_KEY is not configured in the environment.")

    # Normalize input into a list of table dictionaries for unified prompt generation
    if isinstance(tables, str):
        table_list = [{
            "table_name": tables,
            "columns": columns or [],
            "original_filename": ""
        }]
    else:
        table_list = tables

    if not table_list:
        raise ValueError("No table metadata provided for SQL generation.")

    is_multi_table = len(table_list) > 1
    schemas_str = "\n\n".join([format_table_schema_prompt(t) for t in table_list])
    allowed_table_names = [f'"{t["table_name"]}"' for t in table_list]

    if is_multi_table:
        system_prompt = (
            "You are an expert PostgreSQL database assistant.\n"
            "Your task is to write a single, accurate, performant PostgreSQL SELECT query that answers the user's question.\n"
            "CRITICAL RULES:\n"
            f"1. You must ONLY query the following allowed tables: {', '.join(allowed_table_names)}.\n"
            "   Do NOT query any other table or PostgreSQL system schema.\n"
            "2. If answering the question requires data from multiple tables, you MAY write a JOIN or subquery across the allowed tables.\n"
            "3. Only write a SELECT query. Never use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, or stacked queries.\n"
            "4. Wrap table names and column names in double quotes to prevent syntax issues.\n"
            "5. Return ONLY the raw SQL query. Do NOT include markdown code blocks, backticks, comments, or explanations."
        )
    else:
        single_table = table_list[0]["table_name"]
        system_prompt = (
            "You are an expert PostgreSQL database assistant.\n"
            "Your task is to write a single, accurate, performant PostgreSQL SELECT query that answers the user's question.\n"
            "CRITICAL RULES:\n"
            f"1. You must ONLY query the table named \"{single_table}\". Do NOT query any other table or schema.\n"
            "2. Only write a SELECT query. Never use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, or stacked queries.\n"
            "3. Wrap table names and column names in double quotes.\n"
            "4. Return ONLY the raw SQL query. Do NOT include markdown code blocks, backticks, comments, or explanations."
        )

    user_prompt = (
        f"Available PostgreSQL Tables:\n{schemas_str}\n\n"
        f"User Question: {question}\n\n"
        f"PostgreSQL SELECT Query:"
    )

    response = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
        max_tokens=350,
    )

    sql_output = response.choices[0].message.content.strip()
    return sql_output


def generate_natural_language_answer(
    question: str,
    sql_query: str,
    rows: List[Dict[str, Any]],
    row_count: int
) -> str:
    """
    Synthesizes a clear, concise natural language answer grounded exclusively
    in the SQL query results.
    """
    settings = get_settings()
    client = get_groq_client()
    if not client:
        raise ValueError("GROQ_API_KEY is not configured in the environment.")

    # Provide results as JSON data (capped to first 50 rows in the prompt for context length)
    preview_rows = rows[:50]
    data_json = json.dumps(preview_rows, default=str, indent=2)

    system_prompt = (
        "You are a helpful data analyst. You answer questions grounded strictly and exclusively "
        "on the SQL query results provided to you.\n"
        "RULES:\n"
        "1. Answer the user's question accurately using ONLY the provided query results.\n"
        "2. Do NOT hallucinate, guess, or use outside knowledge beyond the data provided.\n"
        "3. If the results are empty (0 rows), clearly state that no matching records were found in the dataset.\n"
        "4. Be concise, professional, and clear."
    )

    user_prompt = (
        f"User Question: {question}\n\n"
        f"Executed SQL Query: {sql_query}\n"
        f"Total Matching Rows: {row_count}\n\n"
        f"Query Results:\n{data_json}\n\n"
        f"Natural Language Answer:"
    )

    response = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=500,
    )

    return response.choices[0].message.content.strip()
