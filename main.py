import calendar
from datetime import datetime, timedelta
import holidays
import getpass
import base64
import csv
import requests
from fuzzywuzzy import process
import re
import matplotlib.pyplot as plt
from jira import JIRA as jira_api
import os  # For environment variables
from typing import List, Dict, Optional
from flask import Flask, request, jsonify

# Discovery for narrowing JQL using Confluence/Jira keywords
from discover_hierarchy import discover_hierarchy, build_refined_jql

app = Flask(__name__)


@app.route('/', methods=['POST'])
def webhook():
    data = request.json
    # Process the data from AppSheet
    response_data = {
        'message': 'Received data!',
        'your_data': data
    }
    return jsonify(response_data)


# Configuration loading
# To keep algorithmic code reusable and isolate changing data/schemas (e.g., company names,
# engineers, field IDs, rankings), we load settings from an external JSON config file.
# Defaults below preserve current behavior if config.json is missing or incomplete.
import json


def load_config(path: str = 'config.json') -> dict:
    """
    Load configuration from a JSON file. If the file is missing or malformed, fall back to
    a set of safe defaults that mirror the existing hardcoded behavior.

    Environment variables can still be used for secrets (see get_credentials).

    Returns a nested dictionary with keys documented in README.md.
    """
    defaults = {
        "company": {
            "name": "VirginMediaO2 Ltd",
            "jira_url": "https://virginmediao2.atlassian.net",
        },
        "data_files": {
            "engineer_names": "engineer_names.csv",
            "leaderboard": "leaderboard.csv",
            "monthly_csv_prefix": "monthly_subtask_summary_data",
            "timelines": "timelines.csv",
        },
        "issue_types": [
            "Bug",
            "Improvement",
            "New Feature",
            "Spike",
            "Epic",
            "Story",
            "Task",
            "Sub-task",
        ],
        "priority_ranking": {
            "Highest": 1,
            "High": 2,
            "Medium": 3,
            "Low": 4,
            "Lowest": 5,
        },
        "issue_ranking": {
            "Epic": 1,
            "Bug": 2,
            "Spike": 3,
            "New Feature": 4,
            "Improvement": 5,
            "Story": 6,
            "Task": 7,
            "Sub-task": 8,
        },
        "custom_fields": {
            # JIRA custom field IDs used by this project. Values are instance-specific and configurable.
            "skills_field": "customfield_10900",
            "workstream_field": "customfield_10952",
            "universe_skill_name": "UniVerse",
        },
        "office_hours": {
            "start_hour": 9,
            "end_hour": 17,
            "country": "GB",  # ISO country code for holidays; GB maps to holidays.UnitedKingdom
        },
    }

    try:
        with open(path, 'r') as f:
            user_cfg = json.load(f)
            # shallow merge for top-level keys
            for k, v in user_cfg.items():
                if isinstance(v, dict) and k in defaults and isinstance(defaults[k], dict):
                    defaults[k].update(v)
                else:
                    defaults[k] = v
    except Exception:
        # Silently fall back to defaults if the config file cannot be read/parsed
        pass
    return defaults


# Resolve configuration into module-level variables used by the rest of the code
_CONFIG = load_config()
EXPECTED_ISSUE_TYPES = _CONFIG.get("issue_types", [])
CSV_FILE_NAME = _CONFIG.get("data_files", {}).get("monthly_csv_prefix", "monthly_subtask_summary_data")
JIRA_URL = _CONFIG.get("company", {}).get("jira_url", "https://virginmediao2.atlassian.net")
PRIORITY_RANKING = _CONFIG.get("priority_ranking", {})
ISSUE_RANKING = _CONFIG.get("issue_ranking", {})
ENGINEER_NAMES_FILE = _CONFIG.get("data_files", {}).get("engineer_names", "engineer_names.csv")
LEADERBOARD_FILE = _CONFIG.get("data_files", {}).get("leaderboard", "leaderboard.csv")
TIMELINES_FILE = _CONFIG.get("data_files", {}).get("timelines", "timelines.csv")
CUSTOM_FIELDS = _CONFIG.get("custom_fields", {})
OFFICE_HOURS = _CONFIG.get("office_hours", {})
# JQL query can be overridden by env var JQL_QUERY for flexibility
JQL_QUERY = os.getenv("JQL_QUERY") or _CONFIG.get("jql_query", 'project = SE ORDER BY Rank')


def read_senior_list(filename: str) -> List[Dict[str, Optional[datetime]]]:
    """
    Reads the senior names from a CSV file and returns a list of dictionaries with name, start date, and end date.
    """
    senior_list = []
    try:
        with open(filename, mode='r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                senior_info = {
                    "name": row["Name"],
                    "start_date": datetime.strptime(row["StartDate"], '%Y-%m-%d'),
                    "end_date": datetime.strptime(row["EndDate"], '%Y-%m-%d') if row["EndDate"] else None
                }
                senior_list.append(senior_info)
    except Exception as e:
        print(f"Error reading or parsing the senior names file: {e}")
    return senior_list


def filter_active_seniors(senior_list: List[Dict[str, Optional[datetime]]], query_date: datetime) -> List[str]:
    """
    Filters the list of seniors based on whether they are active on the given date.
    """
    active_seniors = []
    query_date = datetime.strptime(query_date, "%Y-%m")  # Ensure query_date is correctly formatted as datetime
    for senior in senior_list:
        if senior["start_date"] <= query_date <= (senior["end_date"] if senior["end_date"] else datetime.now()):
            active_seniors.append(senior["name"])
    return active_seniors

# Secure credential handling
def get_credentials():
    """
    Retrieves JIRA credentials from environment variables or prompts the user.
    :return: A tuple containing the username and password.
    """
    username = os.getenv("JIRA_USERNAME") or input("Enter your JIRA username: ")
    password = os.getenv("JIRA_PASSWORD") or getpass.getpass("Enter your JIRA password or API token: ")
    return username, password


# for debugging keys
def print_dict_hierarchy(d: dict, indent=0):
    """
    Recursively prints a dictionary to display its nested structure.
    :param d: The dictionary to print.
    :param indent: The current indentation level.
    """
    for key, value in d.items():
        print('\t' * indent + str(key))
        if isinstance(value, dict):
            print_dict_hierarchy(value, indent + 1)


def is_office_hour(dt, start_hour=9, end_hour=17, holidays=None):
    """
    Check if the datetime is within office hours and not a holiday.
    """
    return dt.weekday() < 5 and start_hour <= dt.hour < end_hour and dt.date() not in holidays


# Function to convert seconds to nearest work-unit equivalent
def seconds_to_work_units(seconds):
    """
    Converts seconds to work units, where 4 hours is considered one work unit.
    :param seconds: The number of seconds.
    :return: The number of work units as an integer.
    """
    hours = seconds / 3600  # Convert seconds to hours
    return int(hours // 4)  # Always round down to the nearest work unit


def convert_month_string_to_datetime(month_str: str) -> datetime:
    """
    Converts a month string in the format 'mon-yy' to a datetime object representing the first day of that month.

    Args:
    month_str (str): A string representing the month and year, formatted as 'yyyy-mm', e.g., '2023-04'.

    Returns:
    datetime: A datetime object set to the first day of the given month and year.
    """
    return datetime.strptime(month_str, "%Y-%m")


def get_monthly_worklog_times(issue):
    """
    Gathers worklog times for each month and categorizes them by workstream.
    :param issue: The issue from which to extract worklog times.
    :return: A dictionary mapping each month to its aggregated worklog times.
    """
    worklogs = issue.fields.worklog.worklogs
    monthly_worklog_times = {}
    # Extract the 'value' from each CustomFieldOption object
    # Use configurable custom field IDs and skill names
    skills_field_id = CUSTOM_FIELDS.get("skills_field", "customfield_10900")
    workstream_field_id = CUSTOM_FIELDS.get("workstream_field", "customfield_10952")
    universe_skill_name = CUSTOM_FIELDS.get("universe_skill_name", "UniVerse")

    tech_skills = [option.value for option in (getattr(issue.fields, skills_field_id, None) or [])]
    # Determine if this worklog is for UniVerse work or not (configurable skill name)
    is_universe = universe_skill_name in tech_skills
    # Derive workstream using configurable field ID
    workstream_field = getattr(issue.fields, workstream_field_id, None)
    worklog_dev_workstream = workstream_field.value if workstream_field else None  # get dev workstream from custom field
    if worklog_dev_workstream:
        worklog_dev_workstream += ' (UniVerse)' if is_universe else ' (non-UniVerse)'
    # print(worklog_dev_workstream)
    for worklog in worklogs:
        worklog_date = datetime.strptime(worklog.started, "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%Y-%m")
        worklog_author = worklog.author.displayName  # use display name as key
        if worklog_author not in monthly_worklog_times:  # initialize new dictionary for new assignee
            monthly_worklog_times[worklog_author] = {}
        if worklog_date not in monthly_worklog_times[worklog_author]:  # initialize new dictionary for new date
            monthly_worklog_times[worklog_author][worklog_date] = {'time_spent': 0,
                                                                   worklog_dev_workstream: {'time_spent': 0}}
        if worklog_dev_workstream not in monthly_worklog_times[worklog_author][worklog_date]:
            monthly_worklog_times[worklog_author][worklog_date][worklog_dev_workstream] = {'time_spent': 0}

        # Correctly increment time_spent at both the date level and the workstream level
        monthly_worklog_times[worklog_author][worklog_date]['time_spent'] += worklog.timeSpentSeconds
        monthly_worklog_times[worklog_author][worklog_date][worklog_dev_workstream][
            'time_spent'] += worklog.timeSpentSeconds
    return monthly_worklog_times


def _get_holidays_calendar(country_code: str):
    """
    Return a holidays calendar instance for the given ISO country code.
    Defaults to UnitedKingdom for 'GB' to preserve existing behavior.
    """
    code = (country_code or 'GB').upper()
    try:
        if code in ('GB', 'UK'):
            return holidays.UnitedKingdom()
        if code == 'US':
            return holidays.UnitedStates()
        if code == 'CA':
            return holidays.Canada()
        if code == 'DE':
            return holidays.Germany()
        if code == 'FR':
            return holidays.France()
        # Fallback generic: some providers support by country code directly
        return holidays.country_holidays(code)
    except Exception:
        # Final fallback to UK to avoid runtime errors if unsupported
        return holidays.UnitedKingdom()


def analyze_issue_transitions(issue):
    """
    Analyzes an issue's changelog to calculate the time spent from 'In Progress' to 'For Peer Review',
    considering only office hours (configurable; default 9am-5pm, Monday-Friday) and excluding regional bank holidays.

    The algorithm is intentionally data-agnostic; changing office hours or holiday region is done via config.json.

    :param issue: The issue whose transitions are to be analyzed.
    :return: Total seconds spent and the count of QA returns.
    """
    # Load office hours and holidays region from configuration
    office_start_hour = int(OFFICE_HOURS.get('start_hour', 9))
    office_end_hour = int(OFFICE_HOURS.get('end_hour', 17))
    country_code = OFFICE_HOURS.get('country', 'GB')
    region_holidays = _get_holidays_calendar(country_code)

    time_to_code = timedelta()
    qa_returns = 0
    in_progress_timestamp = None
    sorted_histories = sorted(issue.changelog.histories, key=lambda history: history.created, reverse=False)

    def within_office_hours(dt):
        # Check if the date is a weekday and within office hours, excluding holidays
        return (dt.weekday() < 5 and
                office_start_hour <= dt.hour < office_end_hour and
                dt.date() not in region_holidays)

    for history in sorted_histories:
        for item in history.items:
            if item.field == 'status':
                print(f'From: {item.fromString}, To: {item.toString}')
                if item.fromString == 'Ready to Develop' and item.toString == 'In Progress':
                    in_progress_timestamp = datetime.strptime(history.created, '%Y-%m-%dT%H:%M:%S.%f%z')
                    print(f"In Progress: {in_progress_timestamp}")  # Debug output
                elif item.toString == 'For Peer Review' and in_progress_timestamp:
                    peer_review_timestamp = datetime.strptime(history.created, '%Y-%m-%dT%H:%M:%S.%f%z')
                    print(f"For Peer Review: {peer_review_timestamp}")  # Debug output
                    if peer_review_timestamp > in_progress_timestamp:
                        # Calculate the duration only within office hours
                        current_time = in_progress_timestamp
                        while current_time < peer_review_timestamp:
                            if within_office_hours(current_time):
                                end_of_hour = current_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                                next_hour_within_office = min(end_of_hour, peer_review_timestamp)
                                time_to_code += next_hour_within_office - current_time
                            current_time += timedelta(hours=1)
                            current_time = current_time.replace(minute=0, second=0, microsecond=0)
                    in_progress_timestamp = None
                elif item.toString != 'Done' and item.fromString == 'Ready for QA':
                    qa_returns += 1

    return time_to_code.total_seconds(), qa_returns


# Function to normalize and clean names
def normalize_name(name):
    """
    Normalizes a name by converting it to lowercase and stripping out non-alphabetic characters.
    :param name: The name to normalize.
    :return: A normalized version of the name.
    """
    return re.sub(r'[^a-z\s]', '', name.lower())


def sorting_key(workstream):
    """
    Defines the sorting key for ordering workstreams, taking into account the suffix.
    :param workstream: The workstream to be sorted.
    :return: A tuple representing the sorting key.
    """
    if workstream is None:
        raise ValueError("workstream should not be None")  # or return a default value if appropriate

    # Check if the workstream ends with " (UniVerse)" or " (non-UniVerse)" and extract the base name accordingly
    if workstream.endswith(" (UniVerse)"):
        base_name = workstream[:-11]  # Remove " (UniVerse)" suffix
        universe_suffix = 2  # UniVerse comes after non-UniVerse
    elif workstream.endswith(" (non-UniVerse)"):
        base_name = workstream[:-15]  # Remove " (non-UniVerse)" suffix
        universe_suffix = 1  # non-UniVerse comes before UniVerse
    else:
        base_name = workstream
        universe_suffix = 0  # Default value for workstreams without these suffixes

    return base_name, universe_suffix, workstream


def plot_pie_charts(summary_data):
    """
    Plots pie charts for workstream distribution per month.
    :param summary_data: Aggregated data for each month to plot.
    """
    for month, data in summary_data.items():
        workstreams = []
        time_spents = []

        # Collect workstreams and their total time spent
        for workstream, workstream_data in data.items():
            if workstream not in ['time_spent', 'time_remaining', *EXPECTED_ISSUE_TYPES]:
                workstreams.append(workstream)
                time_spent = workstream_data.get('time_spent', 0)
                time_spents.append(time_spent)

        # Convert time to work units
        time_spents_in_work_units = [seconds_to_work_units(time) for time in time_spents]
        if sum(time_spents_in_work_units) == 0:  # Avoid plotting if no work has been done
            print(f"No work data available for {month}, skipping pie chart.")
            continue

        # Only convert time_spents_in_work_units to strings when passing to the autopct parameter
        time_spents_in_strings = [str(unit) for unit in time_spents_in_work_units]

        # Create a pie chart
        fig, ax = plt.subplots()
        ax.pie(time_spents_in_work_units, labels=workstreams, autopct=lambda p: '{:.1f}%'.format(p) if p > 0 else '', startangle=90)
        ax.axis('equal')  # Equal aspect ratio ensures the pie is drawn as a circle.

        # Add a title and save the figure
        plt.title(f"Workstream Distribution for {month}")
        plt.savefig(f"pie_chart_{month}.png")
        plt.close(fig)  # Close the figure to avoid displaying it in a non-interactive environment


# Basic Auth setup for JIRA
def create_jira_connection(username, password):
    """
    Creates a connection to the JIRA API using Basic Authentication.
    :param username: JIRA username.
    :param password: JIRA password or API token.
    :return: An authenticated JIRA client object.
    """
    credentials = f"{username}:{password}".encode('utf-8')
    base64_credentials = base64.b64encode(credentials).decode('utf-8')

    # Setup JIRA client with Basic Authentication using Base64 encoded credentials
    options = {
        'server': JIRA_URL,
        'headers': {
            'Authorization': f'Basic {base64_credentials}'
        }
    }
    return jira_api(JIRA_URL, options=options)


def fetch_issues(jira_connector, jql_query):
    """
    Fetches issues from JIRA based on a JQL query.
    :param jira_connector: Authenticated JIRA client object.
    :param jql_query: The JQL query string to execute.
    :return: A list of issues that match the JQL query.
    """
    try:
        return jira_connector.search_issues(jql_query, maxResults=False, expand='changelog,worklog')
    except Exception as e:
        print(f"Error fetching issues: {e}")
        return []


def sort_issues_by_priority(issues):
    """
    Sorts a list of issues based on a custom priority field.
    :param issues: A list of JIRA issue objects.
    :return: A sorted list of JIRA issue objects.
    """
    return sorted(issues, key=lambda x: str(x.fields.customfield_10104), reverse=False)


def leaderboard_output(sorted_leaderboard):
    """
    Outputs the leaderboard information to a CSV file.
    :param sorted_leaderboard: A sorted list of tuples containing engineer names and their data.
    """
    # Output file path is configurable via config.json -> data_files.leaderboard
    with open(LEADERBOARD_FILE, mode='w', newline='') as file:
        writer = csv.writer(file)
        # Updated header row with new metrics
        writer.writerow(["Name", "Total Coding Duration", "QA Returns", "Tasks Completed", "Average Coding Duration", "Throughput (tasks/month)", "QA Return Rate (%)"])
        # Iterate through the sorted leaderboard and write each row
        for name, data in sorted_leaderboard:
            # Calculate QA return rate as a percentage
            qa_return_rate = (data['qa_returns'] / data['tasks_completed'] * 100) if data['tasks_completed'] > 0 else 0
            # Write each row
            writer.writerow([
                name,
                seconds_to_work_units(data['total_time']),
                data['qa_returns'],
                data['tasks_completed'],
                seconds_to_work_units(data['total_time'] / data['tasks_completed']) if data['tasks_completed'] > 0 else 0,
                data['throughput'],  # Directly use the throughput value which is now a float
                f"{qa_return_rate:.2f}%"
            ])
    print(f"Leaderboard data has been written to leaderboard.csv")


def _get_field(issue, field_id_or_name):
    """
    Safely retrieve a field value by id or name from an issue.
    Accepts system names like 'updated', 'created', 'resolutiondate', 'duedate'.
    """
    try:
        if hasattr(issue.fields, field_id_or_name):
            return getattr(issue.fields, field_id_or_name)
    except Exception:
        pass
    # Try normalized lower-case names
    try:
        return getattr(issue.fields, (field_id_or_name or '').lower())
    except Exception:
        return None


def _get_epic_key(issue, fields_map: dict):
    """
    Attempt to extract the epic key for a given issue using discovered epic link candidates
    and common fallbacks.
    """
    candidates = (fields_map or {}).get('epic_link', []) if isinstance(fields_map, dict) else []
    # Try explicit candidates
    for c in candidates:
        val = _get_field(issue, c)
        if isinstance(val, str):
            return val
        # Some instances return an object with key attribute
        try:
            key = getattr(val, 'key', None)
            if key:
                return key
        except Exception:
            pass
    # Fallbacks used in many Jira instances
    for name in ('epicLink', 'customfield_10014'):
        val = _get_field(issue, name)
        if isinstance(val, str):
            return val
        try:
            key = getattr(val, 'key', None)
            if key:
                return key
        except Exception:
            pass
    return None


def generate_timelines_report(issues, fields_map: dict):
    """
    Build a high-level consolidated timelines/progress view per Epic and per Project and
    write to TIMELINES_FILE. Uses discovered fields for start/end/due/progress when available
    and safe fallbacks when not.
    """
    # Aggregation structures
    epic_agg = {}
    proj_agg = {}

    def upd_agg(agg, scope_key, scope_name):
        if scope_key not in agg:
            agg[scope_key] = {
                'name': scope_name,
                'start': None,
                'end': None,
                'last_updated': None,
                'issues': 0,
                'done': 0,
                'assignees': set(),
                'updaters': set(),
                'progress_vals': [],  # tuples (progress, total)
                'created_vals': [],
                'end_candidates': [],
            }
        return agg[scope_key]

    start_fields = (fields_map or {}).get('start_date', [])
    end_fields = (fields_map or {}).get('end_date', [])
    due_fields = (fields_map or {}).get('due_date', [])

    for issue in issues:
        fields = issue.fields
        proj = getattr(getattr(fields, 'project', None), 'key', None) or 'UNKNOWN'
        proj_name = getattr(getattr(fields, 'project', None), 'name', proj)
        epic_key = _get_epic_key(issue, fields_map)
        epic_name = getattr(fields, 'summary', None) if getattr(fields, 'issuetype', None) and getattr(fields.issuetype, 'name', '') == 'Epic' else None
        status = getattr(fields, 'status', None)
        is_done = False
        try:
            cat = getattr(getattr(status, 'statusCategory', None), 'key', '') or getattr(getattr(status, 'statusCategory', None), 'name', '')
            is_done = str(cat).lower() == 'done' or getattr(status, 'name', '').lower() == 'done'
        except Exception:
            pass

        assignee = getattr(getattr(fields, 'assignee', None), 'displayName', None)
        created = _get_field(issue, 'created')
        updated = _get_field(issue, 'updated')
        resolutiondate = _get_field(issue, 'resolutiondate')
        duedate = None
        for d in ['duedate', *due_fields]:
            v = _get_field(issue, d)
            if v:
                duedate = v
                break

        # Start candidates: discovered start fields then created
        start_val = None
        for sf in start_fields:
            v = _get_field(issue, sf)
            if v:
                start_val = v
                break
        if not start_val:
            start_val = created

        # End candidates: discovered end fields, resolutiondate, duedate
        end_val = None
        for ef in end_fields:
            v = _get_field(issue, ef)
            if v:
                end_val = v
                break
        if not end_val:
            end_val = resolutiondate or duedate

        # Progress
        progress = _get_field(issue, 'progress') or _get_field(issue, 'aggregateprogress')
        prog_tuple = None
        try:
            if progress and isinstance(progress, dict):
                prog_tuple = (progress.get('progress'), progress.get('total'))
            else:
                p = getattr(progress, 'progress', None)
                t = getattr(progress, 'total', None)
                if p is not None or t is not None:
                    prog_tuple = (p, t)
        except Exception:
            pass

        # Updater via changelog last history author
        updater_name = None
        try:
            histories = getattr(issue.changelog, 'histories', []) or []
            if histories:
                last = sorted(histories, key=lambda h: h.created)[-1]
                updater_name = getattr(getattr(last, 'author', None), 'displayName', None)
        except Exception:
            pass

        # Update project agg
        pa = upd_agg(proj_agg, proj, proj_name)
        pa['issues'] += 1
        if is_done:
            pa['done'] += 1
        if assignee:
            pa['assignees'].add(assignee)
        if updater_name:
            pa['updaters'].add(updater_name)
        if start_val:
            pa['created_vals'].append(start_val)
        if end_val:
            pa['end_candidates'].append(end_val)
        if updated:
            pa['last_updated'] = max(filter(None, [pa['last_updated'], updated])) if pa['last_updated'] else updated
        if prog_tuple:
            pa['progress_vals'].append(prog_tuple)

        # Update epic agg
        if epic_key:
            ea = upd_agg(epic_agg, epic_key, epic_name or epic_key)
            ea['issues'] += 1
            if is_done:
                ea['done'] += 1
            if assignee:
                ea['assignees'].add(assignee)
            if updater_name:
                ea['updaters'].add(updater_name)
            if start_val:
                ea['created_vals'].append(start_val)
            if end_val:
                ea['end_candidates'].append(end_val)
            if updated:
                ea['last_updated'] = max(filter(None, [ea['last_updated'], updated])) if ea['last_updated'] else updated
            if prog_tuple:
                ea['progress_vals'].append(prog_tuple)

    def pick_min(values):
        try:
            vals = [v for v in values if v]
            return min(vals) if vals else None
        except Exception:
            return None

    def pick_max(values):
        try:
            vals = [v for v in values if v]
            return max(vals) if vals else None
        except Exception:
            return None

    def percent_done(entry):
        # Prefer explicit progress if available
        try:
            if entry['progress_vals']:
                prog = [p for p in entry['progress_vals'] if p and p[1]]
                if prog:
                    sums = [min(100.0, max(0.0, (p[0] or 0) * 100.0 / (p[1] or 1))) for p in prog]
                    return round(sum(sums) / len(sums), 2)
        except Exception:
            pass
        # Fallback: ratio done/issues
        if entry['issues'] > 0:
            return round(100.0 * entry['done'] / entry['issues'], 2)
        return 0.0

    # Finalize start/end by converting candidates
    for agg in (proj_agg, epic_agg):
        for k, e in agg.items():
            e['start'] = pick_min(e['created_vals'])
            e['end'] = pick_max(e['end_candidates'])
            e['percent_done'] = percent_done(e)
            e['assignees'] = sorted(list(e['assignees']))
            e['updaters'] = sorted(list(e['updaters']))

    # Write CSV
    with open(TIMELINES_FILE, mode='w', newline='') as f:
        w = csv.writer(f)
        w.writerow([
            'ScopeType', 'ScopeKey', 'ScopeName', 'StartDate', 'EndDate', 'LastUpdated',
            'PercentDone', 'IssuesCount', 'UniqueAssigneesCount', 'UniqueAssignees',
            'UpdatersCount', 'Updaters'])
        for scope_key, e in proj_agg.items():
            w.writerow([
                'Project', scope_key, e['name'], e['start'], e['end'], e['last_updated'], e['percent_done'],
                e['issues'], len(e['assignees']), "; ".join(e['assignees']), len(e['updaters']), "; ".join(e['updaters'])
            ])
        for scope_key, e in epic_agg.items():
            w.writerow([
                'Epic', scope_key, e['name'], e['start'], e['end'], e['last_updated'], e['percent_done'],
                e['issues'], len(e['assignees']), "; ".join(e['assignees']), len(e['updaters']), "; ".join(e['updaters'])
            ])
    print(f"Timelines report has been written to {TIMELINES_FILE}")


def leaderboard_sort_key(item):
    assignee_data = item[1]
    # Sorting by descending throughput and ascending QA returns
    return -assignee_data['throughput'], assignee_data['qa_returns']


# Main function to orchestrate operations
def main():
    """
    Main function to execute the workflow.
    """
    username, password = get_credentials()
    # Connect to JIRA
    jira_connector = create_jira_connection(username, password)

    # Discovery phase: probe Confluence and Jira to narrow scope based on configured keywords
    try:
        discovery_result = discover_hierarchy(jira_connector, JIRA_URL, (username, password), _CONFIG)
        refined_jql = build_refined_jql(JQL_QUERY, discovery_result)
        if refined_jql != JQL_QUERY:
            print(f"Refined JQL applied: {refined_jql}")
        else:
            print("No discovery refinement applied; using base JQL.")
    except Exception as e:
        print(f"Discovery phase failed ({e}); proceeding with base JQL.")
        refined_jql = JQL_QUERY

    # Fetch issues using refined JQL (or base if discovery did not change it)
    fetched_issues = fetch_issues(jira_connector, refined_jql)
    if fetched_issues:
        sorted_issues = sort_issues_by_priority(fetched_issues)
    else:
        print("No issues found")
        return

    # Generate high-level timelines and resource usage report using discovered fields
    try:
        generate_timelines_report(sorted_issues, getattr(discovery_result, 'fields', {}) if 'discovery_result' in locals() else {})
    except Exception as e:
        print(f"Timelines report generation failed: {e}")

    # Normalize and clean correct names
    # Dynamic loading of senior names
    # Engineer names file is configurable to allow company-specific staffing changes without code edits
    filename = ENGINEER_NAMES_FILE
    senior_list = read_senior_list(filename)
    summary_data = {} # Initialize summary data dictionary for row by row collection of engineer data
    leaderboard = {} # Initialize leaderboard dictionary for engineer metrics

    # Loop through all issues and aggregate data
    for issue in sorted_issues:
        issue_type = issue.fields.issuetype.name
        # print(f'{issue.key}, {issue.fields.customfield_10104}')
        assignee = issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned'
        # Ensure initialization for each assignee
        if assignee not in summary_data:
            summary_data[assignee] = {}

        # Process worklog times for all issues, not just Sub-tasks
        monthly_times = get_monthly_worklog_times(issue)
        # Extract the 'value' from each CustomFieldOption object
        # Use configurable custom field IDs and universe skill name
        skills_field_id = CUSTOM_FIELDS.get("skills_field", "customfield_10900")
        workstream_field_id = CUSTOM_FIELDS.get("workstream_field", "customfield_10952")
        universe_skill_name = CUSTOM_FIELDS.get("universe_skill_name", "UniVerse")
        tech_skills = [option.value for option in (getattr(issue.fields, skills_field_id, None) or [])]
        # Determine if this worklog is for UniVerse work or not
        is_universe = universe_skill_name in tech_skills
        workstream_field = getattr(issue.fields, workstream_field_id, None)
        workstream = workstream_field.value if workstream_field else None  # get dev workstream from custom field
        if workstream:
            workstream += ' (UniVerse)' if is_universe else ' (non-UniVerse)'

        for worklog_assignee, worklog_data in monthly_times.items():
            for month, time_spent_seconds in worklog_data.items():
                if worklog_assignee not in summary_data:
                    summary_data[worklog_assignee] = {}
                # Ensure initialization for each month
                if month not in summary_data[worklog_assignee]:
                    summary_data[worklog_assignee][month] = {type: 0 for type in EXPECTED_ISSUE_TYPES}
                    summary_data[worklog_assignee][month].update({'time_spent': 0, 'time_remaining': 0})

                if workstream not in summary_data[worklog_assignee][month]:
                    summary_data[worklog_assignee][month][workstream] = {'time_spent': 0, 'time_remaining': 0}

                # Update counts and time for the issue's month
                summary_data[worklog_assignee][month][issue_type] += 1
                summary_data[worklog_assignee][month]['time_spent'] += time_spent_seconds['time_spent']
                summary_data[worklog_assignee][month][workstream]['time_spent'] += time_spent_seconds['time_spent']
                #print(f"{issue.key},{worklog_assignee},{summary_data[worklog_assignee][month]['time_spent']}")
        time_to_done, qa_returns = analyze_issue_transitions(issue)

        # Collect data for throughput and QA return rate calculations
        if assignee not in leaderboard:
            leaderboard[assignee] = {
                'total_time': 0,
                'qa_returns': 0,
                'tasks_completed': 0,
                'throughput': 0,  # Initialize as 0
                'months_recorded': 0
            }

        time_to_done, qa_returns = analyze_issue_transitions(issue)
        leaderboard[assignee]['total_time'] += time_to_done
        leaderboard[assignee]['qa_returns'] += qa_returns
        leaderboard[assignee]['tasks_completed'] += 1  # Increment tasks
        leaderboard[assignee]['throughput'] += len(monthly_times)  # Add count of months
        leaderboard[assignee]['months_recorded'] += len(monthly_times.keys())  # Count months

    # Before sorting, convert throughput to average per month
    for assignee, data in leaderboard.items():
        if data['months_recorded'] > 0:
            data['throughput'] /= data[
                'months_recorded']  # Average throughput per month

    # Sort by throughput descending, then by QA returns ascending
    sorted_leaderboard = sorted(leaderboard.items(), key=leaderboard_sort_key)

    # Get all unique workstreams
    # print_dict_hierarchy(summary_data)
    # Fixed function
    all_workstreams = sorted({workstream
                              for engineer_data in summary_data.values()  # engineer level
                              for month_data in engineer_data.values()  # month level
                              for workstream, workstream_info in month_data.items()  # workstream level
                              if workstream is not None and isinstance(workstream_info, dict)
                              and workstream not in ['Bug', 'Improvement', 'New Feature', 'Spike', 'Epic', 'Story',
                                                     'Task',
                                                     'Sub-task', 'time_spent', 'time_remaining']},
                             key=sorting_key)

    header_row = (['Month', 'Engineer', 'Bugs', 'Improvements', 'New Features', 'Spikes', 'Epics', 'Stories', 'Tasks',
                   'Sub-tasks'] + all_workstreams + ['Time Spent (work-units)', 'Time Remaining (work-units)'])

    # Collect and sort all unique months
    all_months = set()
    for assignee_data in summary_data.values():
        all_months.update(assignee_data.keys())
    sorted_months = sorted(all_months, key=lambda x: (datetime.strptime(x, "%Y-%m")))

    file_counter = 0
    while file_counter < 2:
        file_counter += 1
        # Open a new CSV file and write summary data
        with open(CSV_FILE_NAME + str(file_counter) + '.csv', mode='w', newline='') as file:
            writer = csv.writer(file)

            header_row_flag = 1
            # Iterate through each month
            for month in sorted_months:
                if header_row_flag:
                    writer.writerow(header_row)
                    header_row_flag = 0
                if file_counter == 1:
                    header_row_flag = 1
                month_totals = {'Bug': 0, 'Improvement': 0, 'New Feature': 0, 'Spike': 0, 'Epic': 0, 'Story': 0,
                                'Task': 0,
                                'Sub-task': 0}
                month_totals.update({workstream_type: 0 for workstream_type in all_workstreams})
                month_totals.update({'time_spent': 0, 'time_remaining': 0})
                weighted_total_work_units = int(0)
                senior_names = filter_active_seniors(senior_list, convert_month_string_to_datetime(month))
                normalized_correct_names = [normalize_name(name) for name in senior_names]

                # Iterate through each engineer
                for engineer, months_data in summary_data.items():

                    if month in months_data:
                        normalized_engineer_name = normalize_name(engineer)
                        # Use fuzzy matching to find the closest match from correct names
                        best_match, score = process.extractOne(normalized_engineer_name, normalized_correct_names)
                        row = [month, engineer]
                        month_data = months_data[month]

                        # Calculate work units for this engineer and month
                        time_spent_work_units = seconds_to_work_units(month_data.get('time_spent', 0))
                        time_remaining_work_units = seconds_to_work_units(month_data.get('time_remaining', 0))

                        if score > 85:
                            weighted_total_work_units -= int(time_spent_work_units)

                        # Write a row for each engineer for this month
                        for issue_type in EXPECTED_ISSUE_TYPES:
                            row.append(month_data.get(issue_type, 0))
                        # Add workstream time spent values
                        ws_time_spents = []
                        for ws in all_workstreams:
                            ws_time_spents.append(
                                seconds_to_work_units(month_data.get(ws, {'time_spent': 0})['time_spent']))

                        row += ws_time_spents
                        row.extend([time_spent_work_units, time_remaining_work_units])

                        if file_counter == 1:
                            writer.writerow(row)

                        # Update month totals
                        for issue_type in month_totals.keys():
                            if issue_type in ['time_spent', 'time_remaining']:
                                month_totals[issue_type] += month_data.get(issue_type, 0)
                                continue
                            if isinstance(month_data.get(issue_type, 0), int):
                                month_totals[issue_type] += month_data.get(issue_type, 0)
                        for workstream in all_workstreams:
                            month_totals[workstream] += months_data[month].get(workstream, {}).get('time_spent', 0)
                        # month_totals['time_spent'] += month_data.get('time_spent', 0)
                        month_totals['time_remaining'] += month_data.get('time_remaining', 0)

                # Write summary totals for this month
                total_row = [
                                month, 'Totals', month_totals.get('Bug', 0), month_totals.get('Improvement', 0),
                                month_totals.get('New Feature', 0),
                                month_totals.get('Spike', 0), month_totals.get('Epic', 0), month_totals.get('Story', 0),
                                month_totals.get('Task', 0), month_totals.get('Sub-task', 0)] + [
                                seconds_to_work_units(month_totals.get(workstream, 0)) for workstream in
                                all_workstreams]
                total_row.extend([seconds_to_work_units(month_totals.get('time_spent', 0)),
                                  seconds_to_work_units(month_totals.get('time_remaining', 0))])
                writer.writerow(total_row)
                if file_counter == 1:
                    writer.writerow([])

                # Recalculate senior_contribution based on the number of active seniors
                num_active_seniors = len(senior_names)
                senior_contribution = num_active_seniors * 40

                print(
                    f"Senior contribution is calculated with {num_active_seniors} active seniors, resulting in {senior_contribution}.")

                current_month = sorted_months[-1]  # Assuming this is something like '2024-04'
                today = datetime.now()
                month_now, year_now = map(int, current_month.split('-'))

                if f"{year_now}-{month_now:02}" == today.strftime("%Y-%m"):  # Check if processing the current month
                    days_in_month = calendar.monthrange(year_now, month_now)[1]
                    days_through_month = today.day  # Current day of the month
                    partial_month = days_through_month / days_in_month
                else:
                    partial_month = 1  # For past months, use the full contribution

                weighted_total = int(
                    seconds_to_work_units(month_totals['time_spent'])) + weighted_total_work_units + int(
                    senior_contribution * partial_month)

                senior_row = ([
                    month, 'Senior Weighted Totals', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ']) + [' ' for workstream in
                                                                                                 all_workstreams]
                senior_row.extend([weighted_total, ' '])
                if file_counter == 1:
                    writer.writerow(senior_row)
                    if f"{year_now}-{month_now:02}" == today.strftime("%Y-%m"):  # Check if processing the current month
                        estimated_row = ([
                            month, 'Estimation for Month ', ' ', ' ', ' ', ' ', ' ', ' ', ' ', ' ']) + [' ' for
                                                                                                        workstream in
                                                                                                        all_workstreams]
                        estimated_row.extend([(weighted_total + ((1 / partial_month) * weighted_total)), ' '])
                        writer.writerow(estimated_row)
                    writer.writerow([])

            print(f"Monthly summary datafile has been written to {CSV_FILE_NAME + str(file_counter) + '.csv'}")

    leaderboard_output(sorted_leaderboard)
    plot_pie_charts(summary_data)
    print("Analysis and plotting complete.")


if __name__ == '__main__':
    app.run(debug=True)
