"""
Unit tests for database helper functions and identifier quoting.
"""

import unittest
from app.db import sanitize_identifier, quote_ident, map_pandas_dtype_to_postgres


class TestDBHelpers(unittest.TestCase):
    def test_sanitize_identifier(self):
        self.assertEqual(sanitize_identifier("Order ID"), "order_id")
        self.assertEqual(sanitize_identifier("123_Sales-Data!"), "col_123_sales_data")
        self.assertEqual(sanitize_identifier("   spaces   "), "spaces")
        self.assertEqual(sanitize_identifier(""), "col_empty")
        self.assertEqual(sanitize_identifier("customer___name$$$"), "customer_name")

    def test_quote_ident(self):
        self.assertEqual(quote_ident("my_table"), '"my_table"')
        self.assertEqual(quote_ident('my_"weird"_table'), '"my_""weird""_table"')

    def test_map_pandas_dtype_to_postgres(self):
        self.assertEqual(map_pandas_dtype_to_postgres("int64"), "BIGINT")
        self.assertEqual(map_pandas_dtype_to_postgres("float64"), "DOUBLE PRECISION")
        self.assertEqual(map_pandas_dtype_to_postgres("object"), "TEXT")
        self.assertEqual(map_pandas_dtype_to_postgres("bool"), "BOOLEAN")
        self.assertEqual(map_pandas_dtype_to_postgres("datetime64[ns]"), "TIMESTAMP")


if __name__ == "__main__":
    unittest.main()
