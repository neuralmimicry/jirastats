from main import seconds_to_work_units
import unittest

class TestSecondsToWorkUnits(unittest.TestCase):
    def test_exact_work_units(self):
        """
        Test conversion of seconds where the result is an exact number of work units.
        """
        self.assertEqual(seconds_to_work_units(14400), 1)  # 4 hours
        self.assertEqual(seconds_to_work_units(28800), 2)  # 8 hours
        self.assertEqual(seconds_to_work_units(57600), 4)  # 16 hours

    def test_non_exact_work_units(self):
        """
        Test conversion of seconds where the result is not an exact number of work units.
        """
        self.assertEqual(seconds_to_work_units(18000), 1)  # 5 hours, should round to 1 work unit
        self.assertEqual(seconds_to_work_units(1), 0)      # 1 second, should round to 0 work units
        self.assertEqual(seconds_to_work_units(7000), 0)   # Slightly less than 2 hours, should round to 0 work units
        self.assertEqual(seconds_to_work_units(35000), 2)  # Slightly less than 10 hours, should round to 2 work units

    def test_boundary_work_units(self):
        """
        Test boundary conditions where the seconds are right on the edge of rounding up or down.
        """
        self.assertEqual(seconds_to_work_units(21600), 1)  # Exactly 6 hours, rounds down to 1.5 -> 1 work unit
        self.assertEqual(seconds_to_work_units(25200), 1)  # 7 hours, should round down to 1.75 -> 1 work unit


if __name__ == '__main__':
    unittest.main()
