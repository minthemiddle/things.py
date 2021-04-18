#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read from the Things SQLite database using SQL queries."""

import datetime
import os
import plistlib
import re
import sqlite3
import sys
from textwrap import dedent

# --------------------------------------------------
# Core constants
# --------------------------------------------------

# Database filepath

DEFAULT_FILEPATH = os.path.expanduser(
    "~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac"
    "/Things Database.thingsdatabase/main.sqlite"
)

ENVIRONMENT_VARIABLE_WITH_FILEPATH = "THINGSDB"

# Translate app language to database language

TRASHED_TO_FILTER = {True: "trashed = 1", False: "trashed = 0"}

START_TO_FILTER = {
    "Inbox": "start = 0",
    "Anytime": "start = 1",
    "Someday": "start = 2",
}

STATUS_TO_FILTER = {
    "incomplete": "status = 0",
    "canceled": "status = 2",
    "completed": "status = 3",
}

TYPE_TO_FILTER = {"to-do": "type = 0", "project": "type = 1", "heading": "type = 2"}

# Indices

INDICES = ("index", "todayIndex")

# Response modification

COLUMNS_TO_OMIT_IF_NONE = (
    "area",
    "area_title",
    "checklist",
    "heading",
    "heading_title",
    "project",
    "project_title",
    "trashed",
    "tags",
)
COLUMNS_TO_TRANSFORM_TO_BOOL = ("checklist", "tags", "trashed")

# --------------------------------------------------
# Table names
# --------------------------------------------------

TABLE_AREA = "TMArea"
TABLE_AREATAG = "TMAreaTag"
TABLE_CHECKLIST_ITEM = "TMChecklistItem"
TABLE_META = "Meta"
TABLE_TAG = "TMTag"
TABLE_TASK = "TMTask"
TABLE_TASKTAG = "TMTaskTag"
TABLE_SETTINGS = "TMSettings"

# --------------------------------------------------
# Dates
# --------------------------------------------------

# Columns
DATE_CREATED = "creationDate"
DATE_DEADLINE = "dueDate"
DATE_MODIFIED = "userModificationDate"
DATE_START = "startDate"
DATE_STOP = "stopDate"

# Filters
IS_SCHEDULED = f"{DATE_START} IS NOT NULL"
IS_NOT_SCHEDULED = f"{DATE_START} IS NULL"
IS_DEADLINE = f"{DATE_DEADLINE} IS NOT NULL"

# --------------------------------------------------
# Various filters
# --------------------------------------------------

# Type
IS_TODO = TYPE_TO_FILTER["to-do"]
IS_PROJECT = TYPE_TO_FILTER["project"]
IS_HEADING = TYPE_TO_FILTER["heading"]

# Status
IS_INCOMPLETE = STATUS_TO_FILTER["incomplete"]
IS_CANCELED = STATUS_TO_FILTER["canceled"]
IS_COMPLETED = STATUS_TO_FILTER["completed"]

# Start
IS_INBOX = START_TO_FILTER["Inbox"]
IS_ANYTIME = START_TO_FILTER["Anytime"]
IS_SOMEDAY = START_TO_FILTER["Someday"]

# Repeats
IS_RECURRING = "recurrenceRule IS NOT NULL"
IS_NOT_RECURRING = "recurrenceRule IS NULL"
RECURRING_IS_NOT_PAUSED = "instanceCreationPaused = 0"
RECURRING_HAS_NEXT_STARTDATE = "nextInstanceStartDate IS NOT NULL"

# Trashed
IS_NOT_TRASHED = "trashed = 0"
IS_TRASHED = "trashed = 1"


# pylint: disable=R0904,R0902
class Database:
    """
    Access Things SQL database.

    Parameters
    ----------
    filepath : str, optional
        Any valid path of a SQLite database file generated by the Things app.
        If the environment variable `THINGSDB` is set, then use that path.
        Otherwise, access the default database path.

    print_sql : bool, default False
        Print every SQL query performed. Some may contain '?' and ':'
        characters which correspond to SQLite parameter tokens.
        See https://www.sqlite.org/lang_expr.html#varparam
    """

    debug = False

    # pylint: disable=R0913
    def __init__(self, filepath=None, print_sql=False):
        self.filepath = (
            filepath
            or os.getenv(ENVIRONMENT_VARIABLE_WITH_FILEPATH)
            or DEFAULT_FILEPATH
        )
        self.print_sql = print_sql
        if print_sql:
            self.execute_query_count = 0

        # Automated migration to new database location in Things 3.12.6/3.13.1
        # --------------------------------
        try:
            with open(self.filepath) as file:
                if "Your database file has been moved there" in file.readline():
                    self.filepath = DEFAULT_FILEPATH
        except (UnicodeDecodeError, FileNotFoundError, PermissionError):
            pass  # binary file (old database) or doesn't exist
        # --------------------------------

    # Core methods

    def get_tasks(  # pylint: disable=R0914
        self,
        uuid=None,
        type=None,  # pylint: disable=W0622
        status=None,
        start=None,
        area=None,
        project=None,
        heading=None,
        tag=None,
        start_date=None,
        deadline=None,
        trashed: bool = False,
        last=None,
        search_query=None,
        index="index",
        count_only=False,
    ):
        """Get tasks. See `things.api.tasks` for details on parameters."""

        # Overwrites
        start = start and start.title()

        # Validation
        validate("type", type, [None] + list(TYPE_TO_FILTER))  # type: ignore
        validate("status", status, [None] + list(STATUS_TO_FILTER))  # type: ignore
        validate("start", start, [None] + list(START_TO_FILTER))  # type: ignore
        validate("index", index, list(INDICES))
        validate_offset("last", last)

        if tag is not None:
            valid_tags = self.get_tags(titles_only=True)
            validate("tag", tag, [None] + list(valid_tags))

        if uuid:
            if count_only is False and not self.get_tasks(uuid=uuid, count_only=True):
                raise ValueError(f"No such task uuid found: {uuid!r}")

        # Query
        # TK: might consider executing SQL with parameters instead.
        # See: https://docs.python.org/3/library/sqlite3.html#sqlite3.Cursor.execute

        if uuid:
            where_predicate = f"TRUE {make_filter('TASK.uuid', uuid)}"
            order_predicate = None
        else:
            start_filter = START_TO_FILTER.get(start, "")
            status_filter = STATUS_TO_FILTER.get(status, "")
            type_filter = TYPE_TO_FILTER.get(type, "")
            trashed_filter = TRASHED_TO_FILTER.get(trashed, "")

            where_predicate = f"""
                TASK.{IS_NOT_RECURRING}
                AND (PROJECT.title IS NULL OR PROJECT.{IS_NOT_TRASHED})
                AND (HEADPROJ.title IS NULL OR HEADPROJ.{IS_NOT_TRASHED})
                {trashed_filter and f"AND TASK.{trashed_filter}"}
                {type_filter and f"AND TASK.{type_filter}"}
                {start_filter and f"AND TASK.{start_filter}"}
                {status_filter and f"AND TASK.{status_filter}"}
                {make_filter('TASK.uuid', uuid)}
                {make_filter("TASK.area", area)}
                {make_filter("TASK.project", project)}
                {make_filter("TASK.actionGroup", heading)}
                {make_filter("TASK.startDate", start_date)}
                {make_filter(f"TASK.{DATE_DEADLINE}", deadline)}
                {make_filter("TAG.title", tag)}
                {make_date_filter(f"TASK.{DATE_CREATED}", last)}
                {make_search_filter(search_query)}
                """
            order_predicate = f'TASK."{index}"'

        sql_query = make_tasks_sql_query(where_predicate, order_predicate)

        if count_only:
            return self.get_count(sql_query)

        return self.execute_query(sql_query)

    def get_areas(self, uuid=None, tag=None, count_only=False):
        """Get areas. See `api.areas` for details on parameters."""

        # Validation
        if tag is not None:
            valid_tags = self.get_tags(titles_only=True)
            validate("tag", tag, [None] + list(valid_tags))

        if (
            uuid
            and count_only is False
            and not self.get_areas(uuid=uuid, count_only=True)
        ):
            raise ValueError(f"No such area uuid found: {uuid!r}")

        # Query
        sql_query = f"""
            SELECT DISTINCT
                AREA.uuid,
                'area' as type,
                AREA.title,
                CASE
                    WHEN AREA_TAG.areas IS NOT NULL THEN 1
                END AS tags
            FROM
                {TABLE_AREA} AS AREA
            LEFT OUTER JOIN
                {TABLE_AREATAG} AREA_TAG ON AREA_TAG.areas = AREA.uuid
            LEFT OUTER JOIN
                {TABLE_TAG} TAG ON TAG.uuid = AREA_TAG.tags
            WHERE
                TRUE
                {make_filter('TAG.title', tag)}
                {make_filter('AREA.uuid', uuid)}
            ORDER BY AREA."index"
            """

        if count_only:
            return self.get_count(sql_query)

        return self.execute_query(sql_query)

    def get_checklist_items(self, todo_uuid=None):
        """Get checklist items."""
        sql_query = f"""
            SELECT
                CHECKLIST_ITEM.title,
                CASE
                    WHEN CHECKLIST_ITEM.{IS_INCOMPLETE} THEN 'incomplete'
                    WHEN CHECKLIST_ITEM.{IS_CANCELED} THEN 'canceled'
                    WHEN CHECKLIST_ITEM.{IS_COMPLETED} THEN 'completed'
                END AS status,
                date(CHECKLIST_ITEM.stopDate, "unixepoch") AS stop_date,
                'checklist-item' as type,
                CHECKLIST_ITEM.uuid,
                datetime(
                    CHECKLIST_ITEM.{DATE_MODIFIED}, "unixepoch", "localtime"
                ) AS created,
                datetime(
                    CHECKLIST_ITEM.{DATE_MODIFIED}, "unixepoch", "localtime"
                ) AS modified
            FROM
                {TABLE_CHECKLIST_ITEM} AS CHECKLIST_ITEM
            WHERE
                CHECKLIST_ITEM.task = ?
            ORDER BY CHECKLIST_ITEM."index"
            """
        return self.execute_query(sql_query, (todo_uuid,))

    def get_tags(self, title=None, area=None, task=None, titles_only=False):
        """Get tags. See `api.tags` for details on parameters."""

        # Validation
        if title is not None:
            valid_titles = self.get_tags(titles_only=True)
            validate("title", title, [None] + list(valid_titles))

        # Query
        if task:
            return self.get_tags_of_task(task)
        if area:
            return self.get_tags_of_area(area)

        if titles_only:
            sql_query = f'SELECT title FROM {TABLE_TAG} ORDER BY "index"'
            return self.execute_query(sql_query, row_factory=list_factory)

        sql_query = f"""
            SELECT
                uuid, 'tag' AS type, title, shortcut
            FROM
                {TABLE_TAG}
            WHERE
                TRUE
                {make_filter('title', title)}
            ORDER BY "index"
            """

        return self.execute_query(sql_query)

    def get_tags_of_task(self, task_uuid):
        """Get tag titles of task"""
        sql_query = f"""
            SELECT
                TAG.title
            FROM
                {TABLE_TASKTAG} AS TASK_TAG
            LEFT OUTER JOIN
                {TABLE_TAG} TAG ON TAG.uuid = TASK_TAG.tags
            WHERE
                TASK_TAG.tasks = ?
            ORDER BY TAG."index"
            """
        return self.execute_query(
            sql_query, parameters=(task_uuid,), row_factory=list_factory
        )

    def get_tags_of_area(self, area_uuid):
        """Get tag titles for area"""
        sql_query = f"""
            SELECT
                AREA.title
            FROM
                {TABLE_AREATAG} AS AREA_TAG
            LEFT OUTER JOIN
                {TABLE_TAG} AREA ON AREA.uuid = AREA_TAG.tags
            WHERE
                AREA_TAG.areas = ?
            ORDER BY AREA."index"
            """
        return self.execute_query(
            sql_query, parameters=(area_uuid,), row_factory=list_factory
        )

    def get_version(self):
        """Get Things Database version."""

        sql_query = f"SELECT value FROM {TABLE_META} WHERE key = 'databaseVersion'"
        result = self.execute_query(sql_query, row_factory=list_factory)
        plist_bytes = result[0].encode()
        return plistlib.loads(plist_bytes)

    # pylint: disable=R1710
    def get_url_scheme_auth_token(self):
        """Get the Things URL scheme authentication token."""

        sql_query = f"""
            SELECT
                uriSchemeAuthenticationToken
            FROM
                {TABLE_SETTINGS}
            WHERE
                uuid = 'RhAzEf6qDxCD5PmnZVtBZR'
            """
        rows = self.execute_query(sql_query, row_factory=list_factory)
        return rows[0]

    def get_count(self, sql):
        """Count number of results."""
        sql_query = f"""SELECT COUNT(uuid) FROM (\n{sql}\n)"""
        rows = self.execute_query(sql_query, row_factory=list_factory)
        return rows[0]

    # noqa todo: add type hinting for resutl (List[Tuple[str, Any]]?)
    def execute_query(self, sql_query, parameters=(), row_factory=None):
        """Run the actual SQL query"""
        if self.debug is True:
            print(self.filepath)
            print(sql_query)

        if self.print_sql:
            self.execute_query_count += 1
            print(f"/* Query {self.execute_query_count} */")
            if parameters:
                print(f"/* Parameters: {parameters!r} */")
            print()
            print(prettify_sql(sql_query))
            print()

        try:
            # "ro" means read-only
            # See: https://sqlite.org/uri.html#recognized_query_parameters
            uri = f"file:{self.filepath}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)  # pylint: disable=E1101
            connection.row_factory = row_factory or dict_factory
            cursor = connection.cursor()
            cursor.execute(sql_query, parameters)
            rows = cursor.fetchall()
            if self.debug:
                for row in rows:
                    print(row)
            return rows
        except sqlite3.OperationalError as error:  # pylint: disable=E1101
            print(f"Could not query the database at: {self.filepath}.")
            print(f"Details: {error}.")
            sys.exit(2)

    # -------- Utility methods --------

    def last_modified(self):
        """Get last modified time."""
        mtime_seconds = os.path.getmtime(self.filepath)
        return datetime.datetime.fromtimestamp(mtime_seconds)

    def was_modified_today(self):
        """Was task modified today?"""
        last_modified_date = self.last_modified().date()
        todays_date = datetime.datetime.now().date()
        return last_modified_date >= todays_date


# Helper functions


def make_tasks_sql_query(where_predicate=None, order_predicate=None):
    """Make SQL query for Task table"""

    where_predicate = where_predicate or "TRUE"
    order_predicate = order_predicate or 'TASK."index"'

    return f"""
            SELECT DISTINCT
                TASK.uuid,
                CASE
                    WHEN TASK.{IS_TODO} THEN 'to-do'
                    WHEN TASK.{IS_PROJECT} THEN 'project'
                    WHEN TASK.{IS_HEADING} THEN 'heading'
                END AS type,
                CASE
                    WHEN TASK.{IS_TRASHED} THEN 1
                END AS trashed,
                TASK.title,
                CASE
                    WHEN TASK.{IS_INCOMPLETE} THEN 'incomplete'
                    WHEN TASK.{IS_CANCELED} THEN 'canceled'
                    WHEN TASK.{IS_COMPLETED} THEN 'completed'
                END AS status,
                CASE
                    WHEN AREA.uuid IS NOT NULL THEN AREA.uuid
                END AS area,
                CASE
                    WHEN AREA.uuid IS NOT NULL THEN AREA.title
                END AS area_title,
                CASE
                    WHEN PROJECT.uuid IS NOT NULL THEN PROJECT.uuid
                END AS project,
                CASE
                    WHEN PROJECT.uuid IS NOT NULL THEN PROJECT.title
                END AS project_title,
                CASE
                    WHEN HEADING.uuid IS NOT NULL THEN HEADING.uuid
                END AS heading,
                CASE
                    WHEN HEADING.uuid IS NOT NULL THEN HEADING.title
                END AS heading_title,
                TASK.notes,
                CASE
                    WHEN TAG.uuid IS NOT NULL THEN 1
                END AS tags,
                CASE
                    WHEN TASK.{IS_INBOX} THEN 'Inbox'
                    WHEN TASK.{IS_ANYTIME} THEN 'Anytime'
                    WHEN TASK.{IS_SOMEDAY} THEN 'Someday'
                END AS start,
                CASE
                    WHEN CHECKLIST_ITEM.uuid IS NOT NULL THEN 1
                END AS checklist,
                date(TASK.startDate, "unixepoch") AS start_date,
                date(TASK.{DATE_DEADLINE}, "unixepoch") AS deadline,
                date(TASK.stopDate, "unixepoch") AS "stop_date",
                datetime(TASK.{DATE_CREATED}, "unixepoch", "localtime") AS created,
                datetime(TASK.{DATE_MODIFIED}, "unixepoch", "localtime") AS modified,
                TASK.'index',
                TASK.todayIndex AS today_index
            FROM
                {TABLE_TASK} AS TASK
            LEFT OUTER JOIN
                {TABLE_TASK} PROJECT ON TASK.project = PROJECT.uuid
            LEFT OUTER JOIN
                {TABLE_AREA} AREA ON TASK.area = AREA.uuid
            LEFT OUTER JOIN
                {TABLE_TASK} HEADING ON TASK.actionGroup = HEADING.uuid
            LEFT OUTER JOIN
                {TABLE_TASK} HEADPROJ ON HEADING.project = HEADPROJ.uuid
            LEFT OUTER JOIN
                {TABLE_TASKTAG} TAGS ON TASK.uuid = TAGS.tasks
            LEFT OUTER JOIN
                {TABLE_TAG} TAG ON TAGS.tags = TAG.uuid
            LEFT OUTER JOIN
                {TABLE_CHECKLIST_ITEM} CHECKLIST_ITEM
                ON CHECKLIST_ITEM.task = TASK.uuid
            WHERE
                {where_predicate}
            ORDER BY
                {order_predicate}
            """


def dict_factory(cursor, row):
    """
    Convert SQL result into a dictionary.

    See also:
    https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.row_factory
    """
    result = {}
    for index, column in enumerate(cursor.description):
        key, value = column[0], row[index]
        if value is None and key in COLUMNS_TO_OMIT_IF_NONE:
            continue
        if value and key in COLUMNS_TO_TRANSFORM_TO_BOOL:
            value = bool(value)
        result[key] = value
    return result


def list_factory(_cursor, row):
    """
    Convert SQL selects of one column into a list.
    """
    return row[0]


def make_filter(column, value):
    """Return SQL filter 'AND {column} = "{value}"'.

    Special handling if `value` is `bool` or `None`.

    Examples
    --------
    >>> make_filter('title', 'Important')
    'AND title = "Important"'

    >>> make_filter('startDate', True)
    'AND startDate IS NOT NULL'

    >>> make_filter('startDate', False)
    'AND startDate IS NULL'

    >>> make_filter('title', None)
    ''
    """
    default = f'AND {column} = "{value}"'
    # special options
    return {
        None: "",
        False: f"AND {column} IS NULL",
        True: f"AND {column} IS NOT NULL",
    }.get(value, default)


def make_date_filter(date_column, offset):
    """
    Returns SQL filter to limit the date range of the SQL query.

    Parameters
    ----------
    date_column : str
        Name of the column that has date information on a task.

    offset : str or None
        A string comprised of an integer and a single character that can
        be 'd', 'w', or 'y' that determines whether to return all tasks
        for the past X days, weeks, or years.

    Returns
    -------
    str
        A date filter for the SQL query. If `offset == None`, then
        return the empty string.

    Examples
    --------
    >>> make_date_filter('created', '3d')
    "AND datetime(created, 'unixepoch', 'localtime') > datetime('now', '-3 days')"

    >>> make_date_filter('created', None)
    ''

    """

    # Offset not specified
    if offset is None:
        return ""

    validate_offset("offset", offset)

    number, suffix = int(offset[:-1]), offset[-1]

    if suffix == "d":
        modifier = f"-{number} days"
    elif suffix == "w":
        modifier = f"-{number * 7} days"
    elif suffix == "y":
        modifier = f"-{number} years"

    column_datetime = f"datetime({date_column}, 'unixepoch', 'localtime')"
    offset_datetime = f"datetime('now', 'localtime', '{modifier}')"  # type: ignore

    return f"AND {column_datetime} > {offset_datetime}"


def make_search_filter(query: str) -> str:
    """
    Example
    -------
    >>> make_search_filter('dinner')
    'AND (
        TASK.title LIKE "%dinner%"
        OR TASK.notes LIKE "%dinner%"
        OR AREA.title LIKE "%dinner%"
    )'
    """
    if not query:
        return ""

    # noqa todo 'TMChecklistItem.title'
    columns = ["TASK.title", "TASK.notes", "AREA.title"]

    sub_searches = (f'{column} LIKE "%{query}%"' for column in columns)

    return f"AND ({' OR '.join(sub_searches)})"


def prettify_sql(sql_query):
    """
    Make a SQL query easier to read for humans.
    """
    # remove indentation and leading and trailing whitespace
    result = dedent(sql_query).strip()
    # remove empty lines
    return re.sub(r"^$\n", "", result, flags=re.MULTILINE)


def validate(parameter, argument, valid_arguments):
    """
    For a given parameter, check if its argument type is valid.
    If not, then raise `ValueError`.

    Examples
    --------
    >>> validate(
        parameter='status',
        argument='completed',
        valid_arguments=['incomplete', 'completed']
    )

    >>> validate(
        parameter='status',
        argument='XYZXZY',
        valid_arguments=['incomplete', 'completed']
    )
    ValueError: Unrecognized status type: 'XYZXZY'
    Valid status types are ['incomplete', 'completed']
    """
    if argument in valid_arguments:
        return
    message = f"Unrecognized {parameter} type: {argument!r}"
    message += f"\nValid {parameter} types are {valid_arguments}"
    raise ValueError(message)


def validate_offset(parameter, argument):
    """
    For a given offset parameter, check if its argument is valid.
    If not, then raise `ValueError`.

    Examples
    --------
    >>> validate_offset(parameter='last', argument='3d')

    >>> validate_offset(parameter='last', argument='XYZ')
    ValueError: Invalid last argument: 'XYZ'
    ...
    """
    if argument is None:
        return

    if not isinstance(argument, str):
        raise ValueError(
            f"Invalid {parameter} argument: {argument!r}\n"
            f"Please specify a string or None."
        )

    suffix = argument[-1:]  # slicing here to handle empty strings
    if suffix not in ("d", "w", "y"):
        raise ValueError(
            f"Invalid {parameter} argument: {argument!r}\n"
            f"Please specify a string of the format 'X[d/w/y]' "
            "where X is a non-negative integer followed by 'd', 'w', or 'y' "
            "that indicates days, weeks, or years."
        )
