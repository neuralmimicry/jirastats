import unittest
from unittest.mock import Mock, patch
import matplotlib.pyplot as plt

# Assuming the function plot_pie_charts is correctly imported from its module
from main import plot_pie_charts


class TestPlotPieCharts(unittest.TestCase):
    @patch('main.plt.subplots')
    @patch('main.plt.savefig')
    @patch('main.plt.close')
    @patch('main.plt.title')  # Mocking the title function
    @patch('main.seconds_to_work_units', side_effect=lambda x: x // 3600)  # Mock conversion assuming 1 hour per work unit
    def test_plot_pie_charts(self, mock_work_units, mock_title, mock_close, mock_savefig, mock_subplots):
        # Setup mock for subplots to return a figure and an axis
        mock_fig, mock_ax = Mock(), Mock()
        mock_subplots.return_value = (mock_fig, mock_ax)

        # Define test data
        summary_data = {
            'January': {
                'Development': {'time_spent': 14400},  # 4 hours
                'Testing': {'time_spent': 7200},  # 2 hours
                'time_spent': 21600,
                'time_remaining': 3600
            }
        }

        # Call the function
        plot_pie_charts(summary_data)

        # Check that subplots, pie, title, savefig, and close are called correctly
        mock_subplots.assert_called_once()
        mock_ax.pie.assert_called_once()
        mock_ax.axis.assert_called_with('equal')
        mock_title.assert_called_with("Workstream Distribution for January")
        mock_savefig.assert_called_with("pie_chart_January.png")
        plt.close.assert_called_with(mock_fig)


if __name__ == '__main__':
    unittest.main()
