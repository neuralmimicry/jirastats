from main import get_credentials
import unittest
import os
import getpass
from unittest.mock import patch  # Correct import for the patch function


class TestGetCredentials(unittest.TestCase):
    def setUp(self):
        """
        Backup the original values of environment variables before each test.

        This ensures that each test has a clean slate and changes to environment
        variables made during one test do not affect others. It helps in maintaining
        test isolation.
        """
        self.original_jira_username = os.getenv('JIRA_USERNAME')
        self.original_jira_password = os.getenv('JIRA_PASSWORD')

    def tearDown(self):
        """
        Reset environment variables to their original values after each test.

        This cleanup prevents tests from interfering with each other and ensures
        that subsequent tests or operations on the system are not affected by the
        changes made during the tests.
        """
        if self.original_jira_username is None:
            os.unsetenv('JIRA_USERNAME')
        else:
            os.environ['JIRA_USERNAME'] = self.original_jira_username

        if self.original_jira_password is None:
            os.unsetenv('JIRA_PASSWORD')
        else:
            os.environ['JIRA_PASSWORD'] = self.original_jira_password

    def test_get_credentials(self):
        """
        Test that the get_credentials function correctly retrieves credentials
        from environment variables and returns them as a tuple.

        The test sets the environment variables to known values and verifies
        that get_credentials returns a tuple containing these values.
        """
        # Setting the environment variables to known test values
        os.environ['JIRA_USERNAME'] = 'test_user'
        os.environ['JIRA_PASSWORD'] = 'test_password'

        # Expected tuple format for credentials
        expected_credentials = ('test_user', 'test_password')

        # Temporarily mock input and getpass to prevent blocking I/O during the test
        with patch('builtins.input', return_value='test_user'), \
             patch('getpass.getpass', return_value='test_password'):
            # Assert that get_credentials returns the correct tuple of credentials
            self.assertEqual(get_credentials(), expected_credentials)


if __name__ == '__main__':
    unittest.main()
