import unittest

from main import sorting_key


class TestSortingKey(unittest.TestCase):
    def test_sorting_key(self):
        """
        Test that sorting_key correctly extracts base name and assigns appropriate suffix values.
        """
        test_cases = [
            ("Development (UniVerse)", ("Development", 2, "Development (UniVerse)")),
            ("Development (non-UniVerse)", ("Development", 1, "Development (non-UniVerse)")),
            ("Development", ("Development", 0, "Development")),
            ("Quality Assurance", ("Quality Assurance", 0, "Quality Assurance")),
            ("Testing (UniVerse)", ("Testing", 2, "Testing (UniVerse)"))
        ]
        for workstream, expected in test_cases:
            with self.subTest(workstream=workstream):
                self.assertEqual(sorting_key(workstream), expected)

    def test_sorting_key_none_input(self):
        """
        Test that sorting_key raises ValueError when input is None.
        """
        with self.assertRaises(ValueError):
            sorting_key(None)


if __name__ == '__main__':
    unittest.main()
