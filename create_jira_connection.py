import unittest
from unittest.mock import patch, MagicMock
import base64

# Assuming the function create_jira_connection is in a module named jira_module
from main import create_jira_connection

# Constants
JIRA_URL = "https://virginmediao2.atlassian.net"  # Update this to match your function's actual URL


class TestCreateJiraConnection(unittest.TestCase):
    @patch('main.jira_api')
    @patch('main.base64.b64encode')
    def test_create_jira_connection(self, mock_b64encode, mock_jira_api):
        # Mock data
        username = "user"
        password = "pass"
        encoded_credentials = b"encoded_credentials"
        base64_credentials = "encoded_base64_credentials"

        # Set up mocks
        mock_b64encode.return_value = encoded_credentials
        mock_jira_api.return_value = MagicMock()

        # Function call
        jira_client = create_jira_connection(username, password)

        # Assertions to ensure base64 encoding is correct
        mock_b64encode.assert_called_once_with(f"{username}:{password}".encode('utf-8'))
        base64.b64decode(base64_credentials.encode('utf-8'))  # This should not raise an error

        # Assert jira_api called correctly
        mock_jira_api.assert_called_once_with(
            JIRA_URL,
            options={
                'server': JIRA_URL,
                'headers': {'Authorization': f'Basic {base64.b64encode(encoded_credentials).decode("utf-8")}'}
            }
        )

        # Check if the returned value is indeed a mocked JIRA client object
        self.assertIsInstance(jira_client, MagicMock)


if __name__ == '__main__':
    unittest.main()
