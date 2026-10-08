"""BigQuery-bound request handling, with no cloud dependencies.

Stage 5's counterpart to `nightscout_core`, which this module imports rather
than reimplements: authentication, request parsing and response building are
shared with the Stage 3 function, so there is one definition of the credential
scheme no matter which endpoint the phone is pointed at. What differs is the
destination, and that is all this module adds.

Everything here is stdlib-only and side-effect free. Rows are built as plain
Python values and handed to injected callables, so the whole request path can
be exercised offline; the deployed adapter is what turns those values into
protobuf for the Storage Write API and into a Firestore document.

Four ideas carry most of the design:

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
* **"Now" is a document, not a query.** BigQuery bills a 10 MiB minimum per
  table referenced per query, so asking it for the current value costs the
  same whether one row is wanted or a thousand. The current reading is
  published to one Firestore document instead, and because the newest reading
  received is not always the newest reading, that write is conditional.
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

    The table DDL and the protobuf descriptor the Storage Write API needs are
    both generated from this, so the wire format cannot drift from the table.
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

    A batch can legitimately contain the same reading twice. Duplicate rows are
    harmless in the append-only table, but collapsing them here keeps the row
    count in the response honest and makes "which of these is newest" a
    question with one answer.
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


# --------------------------------------------------------------------------
# The current reading
# --------------------------------------------------------------------------

# The reading's own time, in epoch milliseconds, carried on the published
# document. It is what makes a conditional write possible: the value is only
# replaced when the incoming reading is newer than the one already there.
CURRENT_EPOCH_FIELD = "reading_epoch_ms"


def current_document(row: Mapping[str, Any]) -> dict[str, Any]:
    """The document published as "the reading right now".

    A separate shape from the BigQuery row rather than the row itself, for two
    reasons. Firestore has no equivalent of a DATETIME, so the local wall clock
    is published as text alongside the zone it was in, rather than as a
    timestamp that would silently be read back as UTC. And the columns that
    only exist to serve the warehouse — the partition and cluster dates, the
    raw document, the ingest time — are noise to a reader that wants one value.
    """
    return {
        "reading_id": row[IDENTITY_COLUMN],
        CURRENT_EPOCH_FIELD: int(row[ORDER_COLUMN].timestamp() * 1000),
        "reading_time_utc": row[ORDER_COLUMN],
        # Seconds, because this is the value a reader displays; the exact
        # instant is in reading_time_utc and reading_epoch_ms beside it.
        "reading_time_local": row["reading_time_local"].isoformat(sep=" ", timespec="seconds"),
        "local_zone": row["local_zone"],
        "local_offset": row["local_offset"],
        "sgv": row["sgv"],
        "delta": row["delta"],
        "direction": row["direction"],
        "device": row["device"],
        "published_at": row[INGEST_COLUMN],
    }


def supersedes(candidate: Mapping[str, Any], stored: Mapping[str, Any] | None) -> bool:
    """Whether a candidate document should replace what is already published.

    The current value must never go backwards. xDrip resends readings, and a
    batch queued during an outage arrives carrying old timestamps, so "the
    newest reading received" and "the newest reading" are not the same thing.
    Equal timestamps count as superseding, so a replay refreshes the document
    rather than being rejected: the write is the same either way, and treating
    it as a no-op would need the comparison to be exact about values it does
    not otherwise care about.
    """
    if not stored:
        return True
    previous = stored.get(CURRENT_EPOCH_FIELD)
    if previous is None:
        return True
    try:
        return int(candidate[CURRENT_EPOCH_FIELD]) >= int(previous)
    except (TypeError, ValueError):
        return True


# --------------------------------------------------------------------------
# The spreadsheet mirror of the recent day
# --------------------------------------------------------------------------

# Looker Studio issues one BigQuery query per chart, which adds up through the
# day; its Sheets connector does not. So a rolling window of the recent day is
# mirrored into a spreadsheet for the charts that get looked at most, while the
# full history stays in BigQuery for the analysis.
#
# The window is small enough — a day of five-minute readings is under 300 rows —
# that the sheet can serve as its own state. The function reads back what it
# wrote last time, merges, and writes the block again, so it never needs to read
# the history and its BigQuery role stays append-only.

SHEET_HEADER = (
    "reading_time_local",
    "reading_date_local",
    "clock_time",
    "sgv",
    "delta",
    "direction",
    "device",
    "reading_epoch_ms",
)

# The row key, and the column the window is ordered and aged by. Last so that
# the columns a human reads come first.
SHEET_KEY_INDEX = SHEET_HEADER.index("reading_epoch_ms")


def sheet_row(row: Mapping[str, Any]) -> list[Any]:
    """One spreadsheet row, as cell values.

    The local wall clock is split three ways on purpose. The full timestamp is
    what a time series plots against; the date alone groups by day; and the
    clock time alone is what lets one day be overlaid on another, which is the
    chart a glucose dashboard actually wants and which is otherwise awkward to
    derive in Looker.
    """
    local = row["reading_time_local"]
    return [
        local.isoformat(sep=" ", timespec="seconds"),
        local.date().isoformat(),
        local.strftime("%H:%M:%S"),
        row["sgv"],
        row["delta"],
        row["direction"],
        row["device"],
        int(row[ORDER_COLUMN].timestamp() * 1000),
    ]


def _row_key(values: Sequence[Any]) -> int | None:
    """The epoch-millisecond key of an existing sheet row, if it has one.

    Rows read back from a spreadsheet are whatever is in the cells, which may be
    short, empty, or hand-edited, so anything unparseable is treated as having
    no key and is dropped rather than trusted.
    """
    if len(values) <= SHEET_KEY_INDEX:
        return None
    try:
        return int(float(values[SHEET_KEY_INDEX]))
    except (TypeError, ValueError):
        return None


def merge_sheet_window(
    existing: Sequence[Sequence[Any]],
    new_rows: Sequence[Mapping[str, Any]],
    now: datetime,
    window_hours: int = 24,
) -> list[list[Any]]:
    """The full block to write: the recent window, newest first.

    Keyed by reading time, so a reading xDrip resends — or revises after a
    calibration — replaces its own row instead of adding a second one. This is
    the same idempotency as the `entries_current` view, done the only way a
    spreadsheet allows.

    The cutoff is measured from `now` rather than from the newest reading, so
    that missed readings cannot let the window reach further back than it
    claims to. A backlog flushed after days offline therefore adds little or
    nothing here, which is correct: the sheet shows the recent day, and the
    backlog is already in BigQuery.
    """
    cutoff = int((now - timedelta(hours=window_hours)).timestamp() * 1000)

    window: dict[int, list[Any]] = {}
    for values in existing:
        key = _row_key(values)
        if key is not None and key >= cutoff:
            window[key] = list(values)

    for row in new_rows:
        values = sheet_row(row)
        key = values[SHEET_KEY_INDEX]
        if key >= cutoff:
            window[key] = values

    return [window[key] for key in sorted(window, reverse=True)]


def current_block(window: Sequence[Sequence[Any]]) -> list[list[Any]]:
    """The header and the single newest row, for the one-row tab.

    Looker Studio applies row limits after aggregation, which makes "show me
    only the latest reading" awkward to express in a chart. A tab that holds
    exactly one row sidesteps it: any chart built on it is already showing the
    newest reading, with no filter, sort or limit to get wrong.

    Taking `window[0]` is what makes this monotonic for free. The window holds
    the rows already in the sheet as well as the new ones and is ordered newest
    first, so a batch of nothing but old readings leaves the previous newest row
    in place rather than moving the displayed value backwards.
    """
    if not window:
        return []
    return [list(SHEET_HEADER), list(window[0])]


def sheet_range(tab: str, row_count: int, columns: int = len(SHEET_HEADER)) -> str:
    """The A1 range covering the header plus `row_count` rows."""
    last_column = chr(ord("A") + columns - 1)
    return f"{tab}!A1:{last_column}{row_count + 1}"


def stale_range(tab: str, written_rows: int, previous_rows: int) -> str | None:
    """The range below the new block that still holds last time's rows.

    Writing a block only overwrites what it covers, so a window that has shrunk
    — readings aged out faster than new ones arrived — would leave the tail of
    the previous write behind, looking like current data.
    """
    if previous_rows <= written_rows:
        return None
    last_column = chr(ord("A") + len(SHEET_HEADER) - 1)
    return f"{tab}!A{written_rows + 2}:{last_column}{previous_rows + 1}"


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------

# Appends rows to the raw table. Raises to signal failure, which becomes a
# non-2xx response so the phone retries.
RowAppender = Callable[[Sequence[Mapping[str, Any]]], None]

# Publishes the current reading. Returns what happened, for the response
# header: "updated", "stale" when the published value was already newer, or
# "failed". Never raises past the handler, because the document is derived from
# a reading already stored and the next upload republishes it.
CurrentPublisher = Callable[[Mapping[str, Any]], str]

# Mirrors the recent window into the spreadsheet. Takes every row of the batch,
# because unlike the current value, a reading that is not the newest still
# belongs in the window.
#
# Returns what happened — "updated", "skipped" when no spreadsheet is
# configured, or "failed" — together with how many rows the window ended up
# holding. The count is reported because the sheet is the one destination that
# cannot be read from an operator's machine: the Sheets API rejects the
# cloud-platform token that `gcloud` mints, so the response is the only place
# the window's size is visible.
SheetMirror = Callable[[Sequence[Mapping[str, Any]]], tuple[str, int]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Handler:
    """Serves Nightscout-style requests, appending readings through `appender`."""

    secret_payload: Mapping[str, Any]
    appender: RowAppender
    timezone_name: str = "America/Denver"
    publisher: CurrentPublisher | None = None
    mirror: SheetMirror | None = None
    entries_table: str = ""
    current_path: str = ""
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

        # Only the newest reading of the batch is worth publishing as "now",
        # and only it can move the current value forward.
        newest = newest_row(rows)
        current = "skipped"
        if self.publisher is not None and newest is not None:
            current = self.publisher(current_document(newest))

        # Every row of the batch, not just the newest: a reading that lost the
        # race to be "now" still belongs in the window.
        sheet, sheet_rows = "skipped", 0
        if self.mirror is not None:
            sheet, sheet_rows = self.mirror(rows)

        return core.json_response(
            200,
            documents,
            headers={
                "x-xdrip2gcp-table": self.entries_table,
                "x-xdrip2gcp-rows": str(len(rows)),
                "x-xdrip2gcp-documents": str(len(documents)),
                "x-xdrip2gcp-stored": "appended",
                "x-xdrip2gcp-current": current,
                "x-xdrip2gcp-current-path": self.current_path,
                "x-xdrip2gcp-sheet": sheet,
                "x-xdrip2gcp-sheet-rows": str(sheet_rows),
                "x-xdrip2gcp-reading-id": str(newest[IDENTITY_COLUMN]) if newest else "",
            },
        )
