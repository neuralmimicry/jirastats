from main import print_dict_hierarchy
import unittest
from io import StringIO
import sys


class TestPrintDictHierarchy(unittest.TestCase):
    def setUp(self):
        """
        Redirect stdout to capture print statements by using a StringIO object.
        """
        self.captured_output = StringIO()  # Create StringIO object
        self.original_stdout = sys.stdout  # Save a reference to the original standard output
        sys.stdout = self.captured_output  # Redirect stdout to the StringIO object

    def tearDown(self):
        """
        Restore stdout to its original configuration.
        """
        sys.stdout = self.original_stdout  # Reset stdout to its original value

    def test_print_dict_hierarchy(self):
        """
        Test the output of the print_dict_hierarchy function against an expected format.
        """
        test_dict = {
            'a': 1,
            'b': {'c': 2, 'd': {'e': 3}}
        }
        expected_output = 'a\nb\n\tc\n\td\n\t\te\n'

        print_dict_hierarchy(test_dict)

        # Get the entire contents of the StringIO object (i.e., all that was printed so far)
        output = self.captured_output.getvalue()

        # Assert the output is as expected
        self.assertEqual(output, expected_output)


if __name__ == '__main__':
    unittest.main()