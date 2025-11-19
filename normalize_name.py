import unittest
import re

# Assuming the function normalize_name is in the same file or correctly imported from another module
from main import normalize_name


class TestNormalizeName(unittest.TestCase):
    def test_normalize_name(self):
        """
        Test that normalize_name correctly normalizes names by converting them to lowercase
        and removing any non-alphabetic characters.
        """
        test_cases = [
            ("John Doe!", "john doe"),
            ("Mary-Jane", "maryjane"),
            ("O'Neil", "oneil"),
            ("123 Walter", " walter"),
            ("Alice   ", "alice   ")
        ]
        for input_name, expected in test_cases:
            with self.subTest(input_name=input_name):
                self.assertEqual(normalize_name(input_name), expected)


if __name__ == '__main__':
    unittest.main()
