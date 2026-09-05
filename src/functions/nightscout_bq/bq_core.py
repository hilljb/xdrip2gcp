"""BigQuery-bound request handling, with no cloud dependencies.

Stage 5's counterpart to `nightscout_core`, which this module imports rather
than reimplements: authentication, request parsing and response building are
shared with the Stage 3 function, so there is one definition of the credential
scheme no matter which endpoint the phone is pointed at. What differs is the
destination, and that is all this module adds.

Everything here is stdlib-only and side-effect free. Rows are built as plain
Python values and handed to injected callables, so the whole request path can
be exercised offline; the deployed adapter is what turns those values into
protobuf for the Storage Write API and into query parameters for the MERGE.

Three ideas carry most of the design:

* **Identity is (reading time, device), not the whole document.** Hashing
  those two into a `reading_id` means a reading resent by xDrip's retry, or
  resent inside an overlapping batch, is recognisably the same reading. It
  also means a value xDrip revises after a calibration supersedes the original
  instead of sitting beside it, because the view keeps the newest ingest of
  each id.
* **Appends are at-least-once and never mutate.** The raw table only grows,
  duplicates and all, and the `entries_current` view collapses them. A failed
  append therefore just needs a non-2xx response: xDrip keeps the reading
  queued and the retry cannot corrupt anything.
* **Both timezones are stored, not computed.** Every row carries a UTC
  timestamp and the local wall clock, plus the offset and abbreviation that
  wall clock was in, so no query has to convert and the hour that repeats when
  the clocks go back stays interpretable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import nightscout_core as core

# Only entries are stored. The other collections xDrip may post to are
# accepted and dropped: a 404 would make it retry and fill its log with
# errors, and device status carries nothing but a phone battery level.
STORED_COLLECTION = "entries"

SERVER_VERSION = f"{core.SERVER_NAME}-stage5"


@dataclass(frozen=True)
class Column:
    """One column, in the one place the schema is defined.

    The table DDL, the protobuf descriptor the Storage Write API needs, and the
    MERGE that maintains the latest-readings table are all generated from this,
    so they cannot drift apart.
    """

    name: str
    type: str
    required: bool = False

    @property
    def mode(self) -> str:
        """The mode as the table API reports it, for comparing against a live table."""
        return "REQUIRED" if self.required else "NULLABLE"

    @property
    def ddl(self) -> str:
        """The column as DDL, where nullability is spelled `NOT NULL` or omitted."""
        return f"{self.name} {self.type}" + (" NOT NULL" if self.required else "")


# `type` and `date` are avoided as column names: the first shadows a BigQuery
# keyword in some contexts, and the second invites confusion with the DATE
# partition columns. xDrip's own field names are preserved in `raw`.
SCHEMA: tuple[Column, ...] = (
    Column("reading_id", "STRING", required=True),
    Column("reading_time_utc", "TIMESTAMP", required=True),
    Column("reading_date_utc", "DATE", required=True),
    Column("reading_time_local", "DATETIME", required=True),
    Column("reading_date_local", "DATE", required=True),
    Column("local_offset", "STRING"),
    Column("local_zone", "STRING"),
    Column("sgv", "INT64"),
    Column("delta", "FLOAT64"),
    Column("direction", "STRING"),
    Column("device", "STRING"),
    Column("entry_type", "STRING"),
    Column("noise", "INT64"),
    Column("rssi", "INT64"),
    Column("filtered", "FLOAT64"),
    Column("unfiltered", "FLOAT64"),
    Column("date_string", "STRING"),
    Column("sys_time", "STRING"),
    Column("ingest_time", "TIMESTAMP", required=True),
    Column("raw", "JSON"),
)

COLUMN_NAMES: tuple[str, ...] = tuple(column.name for column in SCHEMA)

PARTITION_COLUMN = "reading_date_utc"
CLUSTER_COLUMN = "reading_date_local"
IDENTITY_COLUMN = "reading_id"
ORDER_COLUMN = "reading_time_utc"
INGEST_COLUMN = "ingest_time"

# Where xDrip's field names map onto the typed columns.
INT_FIELDS = {"sgv": "sgv", "noise": "noise", "rssi": "rssi"}
FLOAT_FIELDS = {"delta": "delta", "filtered": "filtered", "unfiltered": "unfiltered"}
STRING_FIELDS = {
    "direction": "direction",
    "device": "device",
    "type": "entry_type",
    "dateString": "date_string",
    "sysTime": "sys_time",
}


class RowError(Exception):
    """Raised when a document cannot be turned into a row."""


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def offset_label(offset: timedelta | None) -> str:
    """Format a UTC offset the way BigQuery and ISO 8601 write it."""
    if offset is None:
        return ""
    total_minutes = int(offset.total_seconds() // 60)
    sign = "-" if total_minutes < 0 else "+"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


@dataclass(frozen=True)
class Stamps:
    """One instant, expressed the several ways a query might want it."""

    utc: datetime
    date_utc: date
    local: datetime
    date_local: date
    offset: str
    zone: str


def stamps(timestamp_ms: int, tz: ZoneInfo) -> Stamps:
    """Expand an epoch-millisecond reading time into stored columns.

    Converting *from* UTC is what keeps the repeated hour at the end of
    daylight saving unambiguous: the wall clock alone appears twice, but the
    offset and abbreviation stored beside it differ.
    """
    moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    local = moment.astimezone(tz)
    return Stamps(
        utc=moment,
        date_utc=moment.date(),
        local=local.replace(tzinfo=None),
        date_local=local.date(),
        offset=offset_label(local.utcoffset()),
        zone=local.tzname() or "",
    )


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def reading_id(timestamp_ms: int, device: str) -> str:
    """A stable identity for a reading.

    Deliberately not a hash of the whole document: xDrip resends readings in
    overlapping batches and may revise a value after a calibration, and both
    should resolve to the row already known rather than to a new one.
    """
    material = f"{timestamp_ms}|{device or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_string(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def build_row(document: Mapping[str, Any], ingest: datetime, tz: ZoneInfo) -> dict[str, Any]:
    """Turn one Nightscout entry into a row of native Python values.

    The adapter converts these to protobuf or to query parameters; keeping the
    canonical form in ordinary Python types is what lets the offline tests
    assert on exactly what would be stored.
    """
    timestamp_ms = core.document_timestamp_ms(document)
    if timestamp_ms is None:
        raise RowError("entry carries no timestamp (expected one of: " + ", ".join(core.TIMESTAMP_FIELDS) + ")")

    when = stamps(timestamp_ms, tz)
    device = _as_string(document.get("device")) or ""

    row: dict[str, Any] = {
        "reading_id": reading_id(timestamp_ms, device),
        "reading_time_utc": when.utc,
        "reading_date_utc": when.date_utc,
        "reading_time_local": when.local,
        "reading_date_local": when.date_local,
        "local_offset": when.offset,
        "local_zone": when.zone,
        "ingest_time": ingest,
        # The document as sent, so a field xDrip adds later is never lost even
        # though it has no column of its own yet.
        "raw": json.dumps(document, sort_keys=True, separators=(",", ":")),
    }

    for source, column in INT_FIELDS.items():
        row[column] = _as_int(document.get(source))
    for source, column in FLOAT_FIELDS.items():
        row[column] = _as_float(document.get(source))
    for source, column in STRING_FIELDS.items():
        row[column] = _as_string(document.get(source))

    return row


def build_rows(
    documents: Sequence[Mapping[str, Any]], ingest: datetime, tz: ZoneInfo
) -> list[dict[str, Any]]:
    """Build rows for a batch, keeping one row per reading identity.

    A batch can legitimately contain the same reading twice; MERGE rejects a
    source that matches a target row more than once, so the batch is collapsed
    here rather than in SQL.
    """
    rows: dict[str, dict[str, Any]] = {}
    for document in documents:
        row = build_row(document, ingest, tz)
        rows[row[IDENTITY_COLUMN]] = row
    return list(rows.values())


def newest_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda row: row[ORDER_COLUMN])


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------


def schema_ddl(columns: Sequence[Column] = SCHEMA) -> str:
    return ",\n".join(f"  {column.ddl}" for column in columns)


def create_entries_ddl(table: str, columns: Sequence[Column] = SCHEMA) -> str:
    """DDL for the raw table: day-partitioned, never expiring.

    No `partition_expiration_days` and no dataset default expiration is what
    makes the data permanent; the absence is deliberate rather than an
    oversight, and `require_partition_filter` is left off because at a few
    megabytes a year the protection would cost more in friction than in money.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n{schema_ddl(columns)}\n)\n"
        f"PARTITION BY {PARTITION_COLUMN}\n"
        f"CLUSTER BY {CLUSTER_COLUMN}\n"
        "OPTIONS (description = 'xDrip CGM readings, one row per upload; "
        "duplicates collapsed by the entries_current view')"
    )


def create_latest_ddl(table: str, keep: int, columns: Sequence[Column] = SCHEMA) -> str:
    """DDL for the small lookup table: same shape, no partitioning."""
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n{schema_ddl(columns)}\n)\n"
        f"OPTIONS (description = 'The {keep} most recent readings, maintained by the function')"
    )


def current_view_body(source: str) -> str:
    """The query behind the view, which is all BigQuery stores of it.

    Kept separate from the DDL so provisioning can compare it against the
    live definition and report honestly about having changed nothing.
    """
    return (
        f"SELECT * FROM {source}\n"
        "QUALIFY ROW_NUMBER() OVER (\n"
        f"  PARTITION BY {IDENTITY_COLUMN} ORDER BY {INGEST_COLUMN} DESC, {ORDER_COLUMN} DESC\n"
        ") = 1"
    )


def create_current_view_sql(view: str, source: str) -> str:
    """The view that makes the append-only table read as one row per reading."""
    return (
        f"CREATE OR REPLACE VIEW {view}\n"
        "OPTIONS (description = 'One row per reading: the newest ingest of each reading_id')\n"
        f"AS {current_view_body(source)}"
    )


def parameter_name(index: int, column: str) -> str:
    return f"r{index}_{column}"


# A JSON column cannot take a bound parameter directly, so the raw document is
# bound as text and parsed in the statement. PARSE_JSON of a NULL is NULL, so
# this needs no special case for a missing value.
def parameter_type(column: Column) -> str:
    return "STRING" if column.type == "JSON" else column.type


def parameter_expression(column: Column, name: str) -> str:
    return f"PARSE_JSON(@{name})" if column.type == "JSON" else f"@{name}"


def merge_latest_sql(
    table: str, row_count: int, keep: int, columns: Sequence[Column] = SCHEMA
) -> str:
    """The one statement that maintains the latest-readings table.

    It reads nothing but the table it maintains, which is what keeps its cost
    flat: BigQuery bills a 10 MiB minimum per table referenced, and this
    references one table holding `keep` rows, so the price of an upload never
    grows with the history in the raw table.

    Combining the new rows with the current contents before ranking is what
    makes it safe in both awkward directions. Replaying a reading changes
    nothing, and a batch that arrives days late loses the ranking to the rows
    already there instead of displacing them.
    """
    names = [column.name for column in columns]
    columns_sql = ", ".join(names)

    new_rows = "\n      UNION ALL ".join(
        "SELECT "
        + ", ".join(
            f"{parameter_expression(column, parameter_name(index, column.name))} AS {column.name}"
            for column in columns
        )
        for index in range(row_count)
    )

    updates = ", ".join(f"{name} = s.{name}" for name in names if name != IDENTITY_COLUMN)
    inserts = ", ".join(f"s.{name}" for name in names)

    return (
        f"MERGE {table} AS t\n"
        "USING (\n"
        "  SELECT * FROM (\n"
        f"      SELECT {columns_sql} FROM {table}\n"
        f"      UNION ALL {new_rows}\n"
        "  )\n"
        f"  QUALIFY ROW_NUMBER() OVER (\n"
        f"    PARTITION BY {IDENTITY_COLUMN} ORDER BY {INGEST_COLUMN} DESC, {ORDER_COLUMN} DESC\n"
        "  ) = 1\n"
        f"  ORDER BY {ORDER_COLUMN} DESC\n"
        f"  LIMIT {keep}\n"
        ") AS s\n"
        f"ON t.{IDENTITY_COLUMN} = s.{IDENTITY_COLUMN}\n"
        f"WHEN MATCHED THEN UPDATE SET {updates}\n"
        f"WHEN NOT MATCHED BY TARGET THEN INSERT ({columns_sql}) VALUES ({inserts})\n"
        "WHEN NOT MATCHED BY SOURCE THEN DELETE"
    )


def merge_parameters(
    rows: Sequence[Mapping[str, Any]], columns: Sequence[Column] = SCHEMA
) -> list[tuple[str, str, Any]]:
    """Name, BigQuery type and value for every parameter the MERGE binds.

    Values from the phone are bound as parameters rather than formatted into
    the statement, so a device name containing a quote is data and never SQL.
    """
    parameters = []
    for index, row in enumerate(rows):
        for column in columns:
            parameters.append(
                (parameter_name(index, column.name), parameter_type(column), row.get(column.name))
            )
    return parameters


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------

# Appends rows to the raw table. Raises to signal failure, which becomes a
# non-2xx response so the phone retries.
RowAppender = Callable[[Sequence[Mapping[str, Any]]], None]

# Maintains the latest-readings table. Returns False when it could not, which
# is reported but never fails the request: the table is derived state and the
# next upload rebuilds it.
LatestUpdater = Callable[[Sequence[Mapping[str, Any]]], bool]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Handler:
    """Serves Nightscout-style requests, appending readings through `appender`."""

    secret_payload: Mapping[str, Any]
    appender: RowAppender
    timezone_name: str = "America/Denver"
    latest_updater: LatestUpdater | None = None
    entries_table: str = ""
    header_name: str = "api-secret"
    max_request_bytes: int = 1048576
    now: Callable[[], datetime] = _utcnow
    _zone: ZoneInfo | None = field(default=None, init=False, repr=False)

    @property
    def zone(self) -> ZoneInfo:
        if self._zone is None:
            self._zone = ZoneInfo(self.timezone_name)
        return self._zone

    def handle(self, request: core.Request) -> core.Response:
        suffix = core.api_suffix(request.path)
        if suffix is None:
            return core.error_response(
                404,
                "Not Found",
                hint=f"endpoints live under {core.API_ROOT}",
                endpoints=list(core.SUPPORTED_ENDPOINTS),
            )

        if suffix in core.STATUS_ENDPOINTS:
            if request.method not in ("GET", "HEAD"):
                return core.error_response(405, "Method Not Allowed", allowed=["GET"])
            return self._status_response()

        if suffix in core.AUTH_CHECK_ENDPOINTS:
            failure = self._authenticate(request)
            if failure is not None:
                return failure
            return core.json_response(200, {"status": "ok", "message": "authorized"})

        collection = suffix.removesuffix(".json")
        if collection not in core.WRITABLE_COLLECTIONS:
            return core.error_response(404, "Not Found", endpoints=list(core.SUPPORTED_ENDPOINTS))

        if request.method != "POST":
            return core.error_response(405, "Method Not Allowed", allowed=["POST"])

        failure = self._authenticate(request)
        if failure is not None:
            return failure

        if collection != STORED_COLLECTION:
            return self._ignored(collection, request)
        return self._store(request)

    def _status_response(self) -> core.Response:
        return core.json_response(
            200,
            {
                "status": "ok",
                "apiEnabled": True,
                "careportalEnabled": False,
                "name": core.SERVER_NAME,
                "version": SERVER_VERSION,
                "apiVersion": core.API_VERSION,
                "serverTime": self.now().isoformat().replace("+00:00", "Z"),
                "settings": {"units": "mg/dl"},
            },
        )

    def _authenticate(self, request: core.Request) -> core.Response | None:
        credential = core.extract_credential(request, self.header_name)
        if credential is None:
            return core.error_response(
                401,
                "Unauthorized",
                hint=f"send sha1_hex(password) in the {self.header_name!r} header",
            )
        if not core.verify_credential(credential, self.secret_payload):
            return core.error_response(401, "Unauthorized")
        return None

    def _ignored(self, collection: str, request: core.Request) -> core.Response:
        """Accept a collection this endpoint does not store.

        The body is still parsed, so a malformed payload is reported as such
        rather than silently accepted, and echoed back the way Nightscout does.
        """
        try:
            documents = core.parse_documents(request.body, self.max_request_bytes)
        except core.PayloadError as error:
            return core.error_response(error.status, error.message)

        return core.json_response(
            200,
            documents,
            headers={
                "x-xdrip2gcp-stored": "ignored",
                "x-xdrip2gcp-collection": collection,
                "x-xdrip2gcp-documents": str(len(documents)),
            },
        )

    def _store(self, request: core.Request) -> core.Response:
        try:
            documents = core.parse_documents(request.body, self.max_request_bytes)
        except core.PayloadError as error:
            return core.error_response(error.status, error.message)

        try:
            rows = build_rows(documents, self.now(), self.zone)
        except RowError as error:
            return core.error_response(400, str(error))

        # Any failure here propagates: the caller turns it into a 5xx so xDrip
        # keeps the reading queued, and a later retry is harmless because
        # duplicate rows are collapsed by the view.
        self.appender(rows)

        latest = "skipped"
        if self.latest_updater is not None:
            latest = "updated" if self.latest_updater(rows) else "failed"

        newest = newest_row(rows)
        return core.json_response(
            200,
            documents,
            headers={
                "x-xdrip2gcp-table": self.entries_table,
                "x-xdrip2gcp-rows": str(len(rows)),
                "x-xdrip2gcp-documents": str(len(documents)),
                "x-xdrip2gcp-stored": "appended",
                "x-xdrip2gcp-latest": latest,
                "x-xdrip2gcp-reading-id": str(newest[IDENTITY_COLUMN]) if newest else "",
            },
        )
