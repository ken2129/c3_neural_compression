"""Boundary, known-value, and invalid-input checks."""
import unittest
from smoke_sum import calculate


class SumTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(calculate(0), {"N": 0, "loop_sum": 0,
                                       "formula_sum": 0, "match": True})

    def test_one(self):
        self.assertEqual(calculate(1), {"N": 1, "loop_sum": 1,
                                       "formula_sum": 1, "match": True})

    def test_thousand(self):
        self.assertEqual(calculate(1000), {"N": 1000, "loop_sum": 500500,
                                          "formula_sum": 500500, "match": True})

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            calculate(-1)

    def test_noninteger_rejected(self):
        with self.assertRaises(TypeError):
            calculate(1.5)


if __name__ == "__main__":
    unittest.main()
