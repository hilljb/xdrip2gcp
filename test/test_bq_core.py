"""Offline tests for the BigQuery function's core. These make no network calls.

Stage 5 was built without live tests by choice, so these cover the two things
that are genuinely easy to get wrong and impossible to eyeball later: the
identity a reading resolves to, and what the local-time columns say during the
hours when Mountain time is not a fixed offset from UTC.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from xdrip2gcp.function_source import bq_core_module

bq = bq_core_module()

DENVER = ZoneInfo("America/Denver")
INGEST = datetime(2026, 9, 4, 23, 59, 0, tzinfo=timezone.utc)

# The reading from Stage 4's verified results, so the tests are anchored to a
# document xDrip really sent.
ENTRY = {
    "date": 1788564814746,
    "dateString": "2026-09-04T17:33:34.746-0600",
    "delta": 0,
    "device": "xDrip-DexcomG5",
    "direction": "Flat",
    "filtered": 0,
    "noise": 1,
    "rssi": 100,
    "sgv": 81,
    "sysTime": "2026-09-04T17:33:34.746-0600",
    "type": "sgv",
    "unfiltered": 0,
}


def row(document=None, ingest=INGEST):
    return bq.build_row(document if document is not None else ENTRY, ingest, DENVER)


def epoch_ms(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


class ReadingIdentityTests(unittest.TestCase):
    def test_is_stable_across_runs(self) -> None:
        self.assertEqual(row()["reading_id"], row()["reading_id"])

    def test_ignores_fields_that_are_not_identity(self) -> None:
        # xDrip revises a reading's value after a calibration. That has to
        # resolve to the same row, so the view can supersede the old value
        # rather than leave two readings for one instant.
        revised = dict(ENTRY, sgv=95, direction="FortyFiveUp", noise=2)
        self.assertEqual(row(revised)["reading_id"], row()["reading_id"])

    def test_differs_by_reading_time(self) -> None:
        later = dict(ENTRY, date=ENTRY["date"] + 300_000)
        self.assertNotEqual(row(later)["reading_id"], row()["reading_id"])

    def test_differs_by_device(self) -> None:
        other = dict(ENTRY, device="xDrip-Libre")
        self.assertNotEqual(row(other)["reading_id"], row()["reading_id"])

    def test_survives_a_missing_device(self) -> None:
        anonymous = {key: value for key, value in ENTRY.items() if key != "device"}
        self.assertTrue(row(anonymous)["reading_id"])


class RowTests(unittest.TestCase):
    def test_maps_xdrip_fields_onto_columns(self) -> None:
        built = row()
        self.assertEqual(built["sgv"], 81)
        self.assertEqual(built["direction"], "Flat")
        self.assertEqual(built["device"], "xDrip-DexcomG5")
        self.assertEqual(built["entry_type"], "sgv")
        self.assertEqual(built["noise"], 1)
        self.assertEqual(built["rssi"], 100)
        self.assertEqual(built["delta"], 0.0)
        self.assertIsInstance(built["delta"], float)

    def test_covers_every_column(self) -> None:
        self.assertEqual(set(row()), set(bq.COLUMN_NAMES))

    def test_keeps_the_document_verbatim(self) -> None:
        # The JSON column is the hedge against xDrip adding a field we have no
        # column for, so it must survive intact.
        surprising = dict(ENTRY, somethingNew={"nested": [1, 2, 3]})
        self.assertEqual(json.loads(row(surprising)["raw"]), surprising)

    def test_absent_fields_are_none_rather_than_zero(self) -> None:
        # A zero would be indistinguishable from a real reading of zero.
        sparse = {"date": ENTRY["date"], "device": "d"}
        built = row(sparse)
        self.assertIsNone(built["sgv"])
        self.assertIsNone(built["delta"])
        self.assertIsNone(built["direction"])

    def test_non_numeric_values_do_not_break_the_row(self) -> None:
        built = row(dict(ENTRY, sgv="unknown", delta=None))
        self.assertIsNone(built["sgv"])
        self.assertIsNone(built["delta"])

    def test_records_the_ingest_time(self) -> None:
        self.assertEqual(row()["ingest_time"], INGEST)

    def test_rejects_a_reading_with_no_timestamp(self) -> None:
        with self.assertRaises(bq.RowError):
            row({"sgv": 100, "device": "d"})

    def test_collapses_a_batch_that_repeats_a_reading(self) -> None:
        # MERGE refuses a source matching one target row twice, so a batch
        # carrying the same reading twice has to be collapsed before SQL.
        rows = bq.build_rows([ENTRY, dict(ENTRY), dict(ENTRY, date=ENTRY["date"] + 300_000)], INGEST, DENVER)
        self.assertEqual(len(rows), 2)

    def test_newest_row_of_a_batch(self) -> None:
        older = dict(ENTRY)
        newer = dict(ENTRY, date=ENTRY["date"] + 300_000)
        rows = bq.build_rows([newer, older], INGEST, DENVER)
        self.assertEqual(bq.newest_row(rows)["reading_id"], row(newer)["reading_id"])


class LocalTimeTests(unittest.TestCase):
    def test_stores_both_clocks_for_one_instant(self) -> None:
        built = row()
        self.assertEqual(built["reading_time_utc"], datetime(2026, 9, 4, 23, 33, 34, 746000, tzinfo=timezone.utc))
        self.assertEqual(built["reading_time_local"], datetime(2026, 9, 4, 17, 33, 34, 746000))
        self.assertEqual(built["local_zone"], "MDT")
        self.assertEqual(built["local_offset"], "-06:00")

    def test_partition_and_cluster_dates_can_differ(self) -> None:
        # An evening reading in Denver is already the next day in UTC, which is
        # exactly why both dates are stored.
        built = row(dict(ENTRY, date=epoch_ms("2026-09-04T20:00:00-06:00")))
        self.assertEqual(str(built["reading_date_utc"]), "2026-09-05")
        self.assertEqual(str(built["reading_date_local"]), "2026-09-04")

    def test_standard_time_in_winter(self) -> None:
        built = row(dict(ENTRY, date=epoch_ms("2026-01-15T12:00:00+00:00")))
        self.assertEqual(built["local_zone"], "MST")
        self.assertEqual(built["local_offset"], "-07:00")
        self.assertEqual(built["reading_time_local"], datetime(2026, 1, 15, 5, 0))

    def test_the_hour_that_happens_twice_stays_distinguishable(self) -> None:
        # Mountain time falls back at 02:00 MDT on 1 November 2026, so 01:30
        # local occurs once in MDT and again an hour later in MST. The wall
        # clock alone is ambiguous; the offset stored beside it is not.
        first = row(dict(ENTRY, date=epoch_ms("2026-11-01T07:30:00+00:00")))
        second = row(dict(ENTRY, date=epoch_ms("2026-11-01T08:30:00+00:00")))

        self.assertEqual(first["reading_time_local"], second["reading_time_local"])
        self.assertEqual(first["local_zone"], "MDT")
        self.assertEqual(second["local_zone"], "MST")
        self.assertEqual(first["local_offset"], "-06:00")
        self.assertEqual(second["local_offset"], "-07:00")
        # And they remain two different readings, an hour apart.
        self.assertNotEqual(first["reading_id"], second["reading_id"])
        self.assertEqual(
            (second["reading_time_utc"] - first["reading_time_utc"]).total_seconds(), 3600
        )

    def test_the_hour_that_never_happens(self) -> None:
        # Clocks jump 02:00 -> 03:00 MST on 8 March 2026, so no reading can
        # land in that hour; converting from UTC simply skips it.
        before = row(dict(ENTRY, date=epoch_ms("2026-03-08T08:59:00+00:00")))
        after = row(dict(ENTRY, date=epoch_ms("2026-03-08T09:01:00+00:00")))
        self.assertEqual(before["reading_time_local"].hour, 1)
        self.assertEqual(after["reading_time_local"].hour, 3)
        self.assertEqual(before["local_zone"], "MST")
        self.assertEqual(after["local_zone"], "MDT")

    def test_offset_labels(self) -> None:
        self.assertEqual(bq.offset_label(None), "")
        self.assertEqual(bq.offset_label(timedelta(hours=-7)), "-07:00")
        self.assertEqual(bq.offset_label(timedelta(hours=5, minutes=30)), "+05:30")
        self.assertEqual(bq.offset_label(timedelta(0)), "+00:00")


class SqlTests(unittest.TestCase):
    def test_entries_table_is_partitioned_and_never_expires(self) -> None:
        ddl = bq.create_entries_ddl("`p.d.entries`")
        self.assertIn(f"PARTITION BY {bq.PARTITION_COLUMN}", ddl)
        self.assertIn(f"CLUSTER BY {bq.CLUSTER_COLUMN}", ddl)
        self.assertNotIn("expiration", ddl.lower())

    def test_required_columns_are_ddl_not_null(self) -> None:
        ddl = bq.create_entries_ddl("`p.d.entries`")
        self.assertIn("reading_id STRING NOT NULL", ddl)
        self.assertIn("sgv INT64,", ddl)
        self.assertNotIn("NULLABLE", ddl)

    def test_view_keeps_the_newest_ingest_of_each_reading(self) -> None:
        body = bq.current_view_body("`p.d.entries`")
        self.assertIn(f"PARTITION BY {bq.IDENTITY_COLUMN}", body)
        self.assertIn(f"ORDER BY {bq.INGEST_COLUMN} DESC", body)

    def test_merge_reads_only_the_table_it_maintains(self) -> None:
        # This is the whole cost argument: referencing the entries table here
        # would make every upload scan the accumulated history.
        sql = bq.merge_latest_sql("`p.d.entries_latest`", row_count=1, keep=2)
        self.assertEqual(sql.count("`p.d.entries_latest`"), 2)
        self.assertNotIn("`p.d.entries`", sql)
        self.assertIn("LIMIT 2", sql)
        self.assertIn("WHEN NOT MATCHED BY SOURCE THEN DELETE", sql)

    def test_merge_binds_every_value_as_a_parameter(self) -> None:
        # A device name is attacker-controlled text as far as this code knows,
        # so it must never be formatted into the statement.
        rows = bq.build_rows([ENTRY], INGEST, DENVER)
        sql = bq.merge_latest_sql("`t`", row_count=len(rows), keep=2)
        parameters = bq.merge_parameters(rows)

        self.assertEqual(len(parameters), len(bq.SCHEMA))
        for name, _, _ in parameters:
            self.assertIn(f"@{name}", sql)
        self.assertNotIn("xDrip-DexcomG5", sql)

    def test_json_column_is_bound_as_text_and_parsed(self) -> None:
        sql = bq.merge_latest_sql("`t`", row_count=1, keep=2)
        self.assertIn("PARSE_JSON(@r0_raw)", sql)
        types = dict((name, type_) for name, type_, _ in bq.merge_parameters(bq.build_rows([ENTRY], INGEST, DENVER)))
        self.assertEqual(types["r0_raw"], "STRING")
        self.assertEqual(types["r0_reading_time_utc"], "TIMESTAMP")
        self.assertEqual(types["r0_reading_date_utc"], "DATE")

    def test_merge_scales_to_a_multi_reading_batch(self) -> None:
        sql = bq.merge_latest_sql("`t`", row_count=3, keep=2)
        self.assertEqual(sql.count("UNION ALL"), 3)
        self.assertIn("@r2_reading_id", sql)


if __name__ == "__main__":
    unittest.main()
