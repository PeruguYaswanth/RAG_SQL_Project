"""
Unit tests for SQL safety and validation rules in app/sql_safety.py
Covers both single-table and multi-table validation scenarios.
"""

import unittest
from app.sql_safety import validate_and_sanitize_sql, strip_markdown_fences, remove_sql_comments


class TestSQLSafety(unittest.TestCase):
    def setUp(self):
        self.table1 = "data_session123_sales"
        self.table2 = "data_session123_customers"
        self.allowed_tables = [self.table1, self.table2]

    def test_valid_single_table_select(self):
        raw_query = f"SELECT region, sum(amount) FROM {self.table1} GROUP BY region ORDER BY sum(amount) DESC"
        is_safe, sanitized_sql, error = validate_and_sanitize_sql(raw_query, self.table1)
        self.assertTrue(is_safe)
        self.assertIsNone(error)
        self.assertTrue(sanitized_sql.startswith("SELECT"))
        self.assertIn("LIMIT 200", sanitized_sql)

    def test_valid_multi_table_join_query(self):
        raw_query = f"""
        SELECT s.product_name, c.customer_name, s.price
        FROM {self.table1} s
        JOIN {self.table2} c ON s.region = c.region
        WHERE s.price > 100
        """
        is_safe, sanitized_sql, error = validate_and_sanitize_sql(raw_query, self.allowed_tables)
        self.assertTrue(is_safe)
        self.assertIsNone(error)
        self.assertTrue(sanitized_sql.startswith("SELECT"))
        self.assertIn("LIMIT 200", sanitized_sql)

    def test_markdown_fence_stripping(self):
        raw_query = f"```sql\nSELECT * FROM {self.table1}\n```"
        is_safe, sanitized_sql, error = validate_and_sanitize_sql(raw_query, self.table1)
        self.assertTrue(is_safe)
        self.assertIsNone(error)
        self.assertNotIn("```", sanitized_sql)
        self.assertIn("LIMIT 200", sanitized_sql)

    def test_existing_limit_preservation(self):
        raw_query = f"SELECT * FROM {self.table1} LIMIT 10"
        is_safe, sanitized_sql, error = validate_and_sanitize_sql(raw_query, self.table1)
        self.assertTrue(is_safe)
        self.assertIsNone(error)
        self.assertTrue(sanitized_sql.endswith("LIMIT 10"))
        self.assertNotIn("LIMIT 200", sanitized_sql)

    def test_reject_non_select(self):
        queries = [
            f"INSERT INTO {self.table1} (col) VALUES (1)",
            f"UPDATE {self.table1} SET col = 2",
            f"DELETE FROM {self.table1}",
            f"DROP TABLE {self.table1}",
            f"ALTER TABLE {self.table1} ADD COLUMN x INT",
            f"TRUNCATE TABLE {self.table1}",
            f"CREATE TABLE test (id int)",
        ]
        for q in queries:
            is_safe, _, error = validate_and_sanitize_sql(q, self.allowed_tables)
            self.assertFalse(is_safe)
            self.assertIsNotNone(error)

    def test_reject_disallowed_keywords_inside_select(self):
        queries = [
            f"SELECT * FROM {self.table1} WHERE id IN (DROP TABLE {self.table1})",
            f"SELECT * FROM {self.table1}; DELETE FROM {self.table1}",
            f"SELECT * FROM {self.table1}; VACUUM FULL",
            f"SELECT * FROM {self.table1}; GRANT ALL ON {self.table1} TO public",
        ]
        for q in queries:
            is_safe, _, error = validate_and_sanitize_sql(q, self.allowed_tables)
            self.assertFalse(is_safe)
            self.assertIsNotNone(error)

    def test_reject_stacked_queries(self):
        query = f"SELECT * FROM {self.table1}; SELECT * FROM pg_users"
        is_safe, _, error = validate_and_sanitize_sql(query, self.allowed_tables)
        self.assertFalse(is_safe)
        self.assertIn("Stacked queries", error)

    def test_reject_unauthorized_third_table(self):
        # table1 and table2 are allowed, but table3 is unauthorized
        unauthorized_table = "data_session999_secret"
        queries = [
            f"SELECT * FROM {unauthorized_table}",
            f"SELECT * FROM {self.table1} JOIN {unauthorized_table} ON {self.table1}.id = {unauthorized_table}.id",
            "SELECT * FROM pg_catalog.pg_tables",
            "SELECT * FROM information_schema.tables",
        ]
        for q in queries:
            is_safe, _, error = validate_and_sanitize_sql(q, self.allowed_tables)
            self.assertFalse(is_safe)
            self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
