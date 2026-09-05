"""Cloud Function entry point for the BigQuery-backed Nightscout endpoint.

An adapter, like the Stage 3 function's: it translates a Flask request into
the framework-free `Request` of `nightscout_core`, supplies the two BigQuery
writers `bq_core` needs, and translates the result back. Everything worth
testing lives in `bq_core`, which has no cloud dependencies.

The two writers are deliberately different in how they fail:

* Appending to the raw table goes through the Storage Write API's default
  stream, which is at-least-once. A failure propagates and becomes a 5xx, so
  xDrip keeps the reading in its own queue and retries; the retry is harmless
  because duplicate rows are collapsed by the `entries_current` view. This is
  what replaces the bucket as a safety net on this path.
* Maintaining the latest-readings table is one MERGE. It is derived state, so
  a failure is reported in a response header and nothing more: the next
  upload reconstructs it.

The protobuf descriptor the Storage Write API needs is built at runtime from
`bq_core.SCHEMA`, the same definition that generates the table DDL, so the
wire format cannot drift from the table. It is proto2 rather than proto3
because proto2's explicit presence is what lets an unset field arrive as NULL
instead of as a zero or an empty string.
"""

from __future__ import annotations

import os
import traceback
from datetime import date
from typing import Any, Mapping, Sequence

from google.cloud import bigquery, bigquery_storage_v1
from google.cloud.bigquery_storage_v1 import types, writer
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

import bq_core
import nightscout_core as core

PROJECT_ENV = "XDRIP2GCP_BQ_PROJECT"
DATASET_ENV = "XDRIP2GCP_BQ_DATASET"
ENTRIES_ENV = "XDRIP2GCP_BQ_ENTRIES_TABLE"
LATEST_ENV = "XDRIP2GCP_BQ_LATEST_TABLE"
LATEST_ROWS_ENV = "XDRIP2GCP_BQ_LATEST_ROWS"
TIMEZONE_ENV = "XDRIP2GCP_BQ_TIMEZONE"
LOCATION_ENV = "XDRIP2GCP_BQ_LOCATION"
SECRET_ENV = "NIGHTSCOUT_SECRET"
HEADER_ENV = "XDRIP2GCP_AUTH_HEADER"
MAX_BYTES_ENV = "XDRIP2GCP_MAX_REQUEST_BYTES"

EPOCH = date(1970, 1, 1)
MESSAGE_NAME = "XdripEntry"

# BigQuery column type -> protobuf field type. The three that are not
# self-evident: a TIMESTAMP is microseconds since the epoch, a DATE is days
# since the epoch, and a JSON column takes the document as text.
PROTO_TYPES = {
    "STRING": descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    "JSON": descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    "DATETIME": descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    "TIMESTAMP": descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    "DATE": descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    "INT64": descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    "FLOAT64": descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE,
}

# Built once per instance: the clients, the credential digest and the compiled
# message class are all worth keeping warm.
_handler: bq_core.Handler | None = None
_write_client: bigquery_storage_v1.BigQueryWriteClient | None = None
_query_client: bigquery.Client | None = None
_message_class: Any = None


class ConfigurationError(Exception):
    """Raised when the function's environment is incomplete."""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"environment variable {name} is not set")
    return value


def _table_id(table_env: str) -> str:
    return f"{_require_env(PROJECT_ENV)}.{_require_env(DATASET_ENV)}.{_require_env(table_env)}"


# --------------------------------------------------------------------------
# Protobuf
# --------------------------------------------------------------------------


def _descriptor_proto() -> descriptor_pb2.DescriptorProto:
    """Describe `bq_core.SCHEMA` as a proto2 message with all fields optional."""
    proto = descriptor_pb2.DescriptorProto()
    proto.name = MESSAGE_NAME
    for number, column in enumerate(bq_core.SCHEMA, start=1):
        field = proto.field.add()
        field.name = column.name
        field.number = number
        field.type = PROTO_TYPES[column.type]
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    return proto


def _message_type() -> Any:
    global _message_class
    if _message_class is None:
        file_proto = descriptor_pb2.FileDescriptorProto()
        file_proto.name = "xdrip2gcp_entries.proto"
        # Explicit presence: an unset field must reach BigQuery as NULL, which
        # proto3's implicit presence would turn into a zero or empty string.
        file_proto.syntax = "proto2"
        file_proto.message_type.add().CopyFrom(_descriptor_proto())

        pool = descriptor_pool.DescriptorPool()
        pool.Add(file_proto)
        _message_class = message_factory.GetMessageClass(
            pool.FindMessageTypeByName(MESSAGE_NAME)
        )
    return _message_class


def _proto_value(column: bq_core.Column, value: Any) -> Any:
    if column.type == "TIMESTAMP":
        return int(value.timestamp() * 1_000_000)
    if column.type == "DATE":
        return (value - EPOCH).days
    if column.type == "DATETIME":
        return value.isoformat(sep=" ")
    return value


def _serialize(row: Mapping[str, Any]) -> bytes:
    """Serialize one row, leaving absent values unset so they store as NULL."""
    message = _message_type()()
    for column in bq_core.SCHEMA:
        value = row.get(column.name)
        if value is None:
            continue
        setattr(message, column.name, _proto_value(column, value))
    return message.SerializeToString()


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------


def _writer_client() -> bigquery_storage_v1.BigQueryWriteClient:
    global _write_client
    if _write_client is None:
        _write_client = bigquery_storage_v1.BigQueryWriteClient()
    return _write_client


def _append_rows(rows: Sequence[Mapping[str, Any]]) -> None:
    """Append rows to the raw table through the default write stream.

    The stream is opened per request rather than kept on the instance: uploads
    are five minutes apart, so a cached stream would usually be found stale,
    and the reconnect would cost more than opening a fresh one.
    """
    if not rows:
        return

    client = _writer_client()
    parent = client.table_path(
        _require_env(PROJECT_ENV), _require_env(DATASET_ENV), _require_env(ENTRIES_ENV)
    )

    template = types.AppendRowsRequest()
    template.write_stream = f"{parent}/_default"
    proto_data = types.AppendRowsRequest.ProtoData()
    proto_data.writer_schema = types.ProtoSchema(proto_descriptor=_descriptor_proto())
    template.proto_rows = proto_data

    stream = writer.AppendRowsStream(client, template)
    try:
        proto_rows = types.ProtoRows()
        for row in rows:
            proto_rows.serialized_rows.append(_serialize(row))

        request = types.AppendRowsRequest()
        request.proto_rows = types.AppendRowsRequest.ProtoData(rows=proto_rows)
        # A proto-plus message, so an unset `error` reads as a default Status
        # with code 0 rather than needing a presence check.
        response = stream.send(request).result()
        if response.error.code != 0:
            raise RuntimeError(f"append failed: {response.error.message}")
    finally:
        stream.close()


def _query_runner() -> bigquery.Client:
    global _query_client
    if _query_client is None:
        _query_client = bigquery.Client(project=_require_env(PROJECT_ENV))
    return _query_client


def _update_latest(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Fold the new readings into the latest-readings table.

    Reports failure rather than raising: the table is derived from data already
    safely appended, so the request has succeeded either way and the next
    upload will reconcile it.
    """
    if not rows:
        return True

    table = f"`{_table_id(LATEST_ENV)}`"
    keep = int(os.environ.get(LATEST_ROWS_ENV, "2"))
    sql = bq_core.merge_latest_sql(table, len(rows), keep)
    parameters = [
        bigquery.ScalarQueryParameter(name, type_, value)
        for name, type_, value in bq_core.merge_parameters(rows)
    ]

    try:
        job = _query_runner().query(
            sql,
            job_config=bigquery.QueryJobConfig(query_parameters=parameters),
            location=os.environ.get(LOCATION_ENV) or None,
        )
        job.result()
        return True
    except Exception:  # noqa: BLE001 - derived state; log and carry on
        print(f"latest-readings update failed:\n{traceback.format_exc()}")
        return False


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _build_handler() -> bq_core.Handler:
    payload = core.load_secret_payload(_require_env(SECRET_ENV))
    return bq_core.Handler(
        secret_payload=payload,
        appender=_append_rows,
        latest_updater=_update_latest,
        timezone_name=_require_env(TIMEZONE_ENV),
        entries_table=_table_id(ENTRIES_ENV),
        header_name=os.environ.get(HEADER_ENV, "api-secret"),
        max_request_bytes=int(os.environ.get(MAX_BYTES_ENV, "1048576")),
    )


def _get_handler() -> bq_core.Handler:
    global _handler
    if _handler is None:
        _handler = _build_handler()
    return _handler


def nightscout_bq(request: Any):
    """HTTP entry point; `--entry-point=nightscout_bq` targets this function."""
    try:
        handler = _get_handler()
        core_request = core.Request.create(
            method=request.method,
            path=request.path,
            headers=dict(request.headers),
            query=request.args.to_dict(),
            body=request.get_data(cache=False) or b"",
        )
        response = handler.handle(core_request)
    except (ConfigurationError, core.SecretPayloadError) as error:
        print(f"configuration error: {error}")
        response = core.error_response(500, "Server misconfigured")
    except Exception:  # noqa: BLE001 - never leak a traceback to the caller
        # Includes a failed append, which must be a 5xx: xDrip retries on one,
        # and that retry is the only thing standing between a transient
        # BigQuery error and a lost reading.
        print(f"unhandled error:\n{traceback.format_exc()}")
        response = core.error_response(503, "Storage unavailable; retry later")

    return response.body, response.status, response.headers
