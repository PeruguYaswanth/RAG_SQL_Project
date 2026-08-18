"""
SQL Safety and Validation Module.

SAFETY NOTICE:
In a production deployment, the database user in DATABASE_URL should ideally be configured
with read-only (SELECT-only) permissions and table-level access controls in PostgreSQL.
The keyword, table-isolation, and statement validation rules implemented in this module
serve as an active defense-in-depth security layer to prevent malicious or accidental
execution of destructive, stacked, or cross-tenant queries.
"""

import re
import logging
from typing import Tuple, Optional, Set, List, Union

logger = logging.getLogger(__name__)

# Disallowed SQL keywords that could modify state, schema, or permissions
DISALLOWED_KEYWORDS = [
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "CREATE",
    "REPLACE",
    "VACUUM",
    "COPY",
]

# Regex pattern for disallowed keywords with word boundaries
DISALLOWED_PATTERN = re.compile(
    r"\b(" + "|".join(DISALLOWED_KEYWORDS) + r")\b",
    re.IGNORECASE
)

# Regex to detect stacked queries: a semicolon followed by any non-whitespace characters
STACKED_QUERY_PATTERN = re.compile(r";\s*\S+")

# Regex to find table references in FROM and JOIN clauses
# Captures standard identifier tokens: table_name, schema.table_name, "table_name", etc.
TABLE_REFERENCE_PATTERN = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-zA-Z0-9_\.\"]+)",
    re.IGNORECASE
)

# Common PostgreSQL system tables and schemas to explicitly block
SYSTEM_TABLE_PATTERNS = [
    r"\bpg_",
    r"\binformation_schema\b",
    r"\bpg_catalog\b"
]


def strip_markdown_fences(query: str) -> str:
    """
    Remove markdown code fences (e.g., ```sql ... ``` or ``` ... ```).
    """
    cleaned = query.strip()
    # Remove leading ```sql or ```
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def remove_sql_comments(query: str) -> str:
    """
    Remove standard single-line (-- ...) and multi-line (/* ... */) SQL comments.
    """
    # Remove multi-line comments
    query = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    # Remove single-line comments
    query = re.sub(r"--.*$", "", query, flags=re.MULTILINE)
    return query.strip()


def extract_table_references(query: str) -> Set[str]:
    """
    Extract table names referenced in FROM and JOIN clauses.
    Returns cleaned, lowercase table names without quotes or schema prefixes.
    """
    tables = set()
    matches = TABLE_REFERENCE_PATTERN.findall(query)
    for match in matches:
        # Strip quotes and schema qualifiers (e.g., public."my_table" -> my_table)
        clean_name = match.strip().strip('"').strip("'")
        if "." in clean_name:
            clean_name = clean_name.split(".")[-1].strip('"')
        if clean_name:
            tables.add(clean_name.lower())
    return tables


def validate_and_sanitize_sql(
    raw_query: str,
    allowed_tables: Union[str, List[str], Set[str]]
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validates that an LLM-generated SQL query is safe to execute against PostgreSQL.

    Rules enforced:
    a. Strips markdown code fences if present.
    b. Rejects if the query does not start with SELECT (case-insensitive).
    c. Rejects if it contains dangerous keywords (INSERT, UPDATE, DELETE, DROP, etc.).
    d. Rejects stacked queries (semicolon followed by non-whitespace).
    e. Rejects references to any table NOT present in allowed_tables
       (blocks cross-session querying, querying tables outside session scope, and system catalog access).
    f. Appends LIMIT 200 if no LIMIT clause is present.

    Args:
        raw_query: The raw SQL string returned by the LLM.
        allowed_tables: A single table name (str) or a collection of allowed table names (List[str] / Set[str]).

    Returns:
        Tuple of (is_safe: bool, sanitized_query_or_none: Optional[str], error_reason: Optional[str])
    """
    if not raw_query or not raw_query.strip():
        reason = "Empty SQL query received."
        logger.warning(f"SQL validation rejected: {reason}")
        return False, None, reason

    # Normalize allowed_tables to a set of clean, lowercase table strings
    if isinstance(allowed_tables, str):
        allowed_set = {allowed_tables.lower().strip('"')}
    else:
        allowed_set = {str(t).lower().strip('"') for t in allowed_tables if str(t).strip()}

    if not allowed_set:
        reason = "No allowed tables configured for query execution."
        logger.warning(f"SQL validation rejected: {reason}")
        return False, None, reason

    # a. Strip markdown code fences
    cleaned_query = strip_markdown_fences(raw_query)

    # Remove comments for validation inspection
    uncommented_query = remove_sql_comments(cleaned_query)
    if not uncommented_query:
        reason = "Query contains only comments or whitespace."
        logger.warning(f"SQL validation rejected: {reason}")
        return False, None, reason

    # b. Must start with SELECT
    if not re.match(r"^SELECT\b", uncommented_query, re.IGNORECASE):
        reason = "Query must start with SELECT."
        logger.warning(f"SQL validation rejected: {reason} Query: {uncommented_query[:100]}")
        return False, None, reason

    # c. Reject disallowed destructive / administrative keywords
    disallowed_match = DISALLOWED_PATTERN.search(uncommented_query)
    if disallowed_match:
        keyword = disallowed_match.group(0).upper()
        reason = f"Dangerous keyword '{keyword}' is prohibited."
        logger.warning(f"SQL validation rejected: {reason} Query: {uncommented_query[:100]}")
        return False, None, reason

    # Check for stacked queries (semicolon followed by more SQL)
    if STACKED_QUERY_PATTERN.search(uncommented_query):
        reason = "Stacked queries separated by semicolons are prohibited."
        logger.warning(f"SQL validation rejected: {reason} Query: {uncommented_query[:100]}")
        return False, None, reason

    # d. System table check
    for sys_pattern in SYSTEM_TABLE_PATTERNS:
        if re.search(sys_pattern, uncommented_query, re.IGNORECASE):
            reason = "Access to system tables or schemas is prohibited."
            logger.warning(f"SQL validation rejected: {reason} Query: {uncommented_query[:100]}")
            return False, None, reason

    # e. Multi-table session isolation check
    referenced_tables = extract_table_references(uncommented_query)

    if referenced_tables:
        for tbl in referenced_tables:
            if tbl not in allowed_set:
                reason = (
                    f"Query references unauthorized table '{tbl}'. "
                    f"Allowed tables: {sorted(list(allowed_set))}."
                )
                logger.warning(f"SQL validation rejected: {reason} Query: {uncommented_query[:100]}")
                return False, None, reason
    else:
        # If regex didn't extract FROM/JOIN, ensure at least one allowed table appears in the query text
        found_any = any(tbl in uncommented_query.lower() for tbl in allowed_set)
        if not found_any:
            reason = f"Query must reference at least one authorized table from: {sorted(list(allowed_set))}."
            logger.warning(f"SQL validation rejected: {reason} Query: {uncommented_query[:100]}")
            return False, None, reason

    # Strip any trailing semicolon from query before executing or adding LIMIT
    sanitized_sql = cleaned_query.rstrip().rstrip(";").strip()

    # f. Check for LIMIT clause; append LIMIT 200 if absent
    if not re.search(r"\bLIMIT\s+\d+", sanitized_sql, re.IGNORECASE):
        sanitized_sql = f"{sanitized_sql} LIMIT 200"

    return True, sanitized_sql, None
