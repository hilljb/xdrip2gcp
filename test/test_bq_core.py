"""Offline tests for the BigQuery function's core. These make no network calls.

Stage 5 was built without live tests by choice, so these cover the things that
are genuinely easy to get wrong and impossible to eyeball later: the identity a
reading resolves to, what the local-time columns say during the hours when
Mountain time is not a fixed offset from UTC, and whether a late-arriving
reading can drag the published current value backwards.
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


class CurrentValueTests(unittest.TestCase):
    """The document published to Firestore as "the reading right now"."""

    def document(self, entry: dict) -> dict:
        return bq.current_document(row(entry))

    def test_carries_what_a_reader_needs_and_not_the_warehouse_columns(self) -> None:
        document = self.document(ENTRY)
        self.assertEqual(document["sgv"], ENTRY["sgv"])
        self.assertEqual(document["direction"], ENTRY["direction"])
        self.assertEqual(document["device"], ENTRY["device"])
        # The partition and cluster dates, the ingest time and the raw document
        # exist to serve BigQuery; a reader asking for one value gets none of it.
        for absent in ("reading_date_utc", "reading_date_local", "raw", "ingest_time"):
            self.assertNotIn(absent, document)

    def test_local_time_is_text_beside_its_zone(self) -> None:
        # Firestore has no DATETIME, and a naive datetime would be stored as a
        # timestamp and read back as UTC, silently seven hours wrong.
        document = self.document(ENTRY)
        self.assertIsInstance(document["reading_time_local"], str)
        # Seconds, not microseconds: this is the string a reader displays.
        self.assertEqual(document["reading_time_local"], "2026-09-04 17:33:34")
        self.assertIn(document["local_zone"], ("MST", "MDT"))
        self.assertEqual(document["reading_time_utc"].tzinfo, timezone.utc)

    def test_epoch_matches_the_reading_not_the_ingest(self) -> None:
        document = self.document(ENTRY)
        self.assertEqual(document[bq.CURRENT_EPOCH_FIELD], ENTRY["date"])

    def test_first_reading_is_published(self) -> None:
        self.assertTrue(bq.supersedes(self.document(ENTRY), None))
        self.assertTrue(bq.supersedes(self.document(ENTRY), {}))

    def test_a_newer_reading_replaces_an_older_one(self) -> None:
        older = self.document(ENTRY)
        newer = self.document(dict(ENTRY, date=ENTRY["date"] + 300_000))
        self.assertTrue(bq.supersedes(newer, older))

    def test_a_late_reading_never_moves_the_value_backwards(self) -> None:
        # A batch queued during an outage arrives carrying old timestamps. It
        # belongs in the history, but it is not "now".
        current = self.document(ENTRY)
        stale = self.document(dict(ENTRY, date=ENTRY["date"] - 3_600_000))
        self.assertFalse(bq.supersedes(stale, current))

    def test_a_replay_refreshes_rather_than_being_rejected(self) -> None:
        document = self.document(ENTRY)
        self.assertTrue(bq.supersedes(document, document))

    def test_a_stored_document_without_an_epoch_is_replaced(self) -> None:
        # Nothing should ever write one, but a hand-edited document must not be
        # able to freeze the current value permanently.
        self.assertTrue(bq.supersedes(self.document(ENTRY), {"sgv": 100}))
        self.assertTrue(bq.supersedes(self.document(ENTRY), {bq.CURRENT_EPOCH_FIELD: "corrupt"}))


class SheetWindowTests(unittest.TestCase):
    """The rolling 24-hour window mirrored into the spreadsheet.

    The sheet is the window's only storage, so this merge is the whole design:
    get it wrong and the dashboard either grows duplicates forever or quietly
    reaches further back than the day it claims to show.
    """

    # Noon the day after the anchor reading, so the fixture sits comfortably
    # inside the window and the boundary cases are deliberate.
    NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)

    def reading(self, minutes_before_now: int, **overrides):
        taken = self.NOW - timedelta(minutes=minutes_before_now)
        return row(dict(ENTRY, date=int(taken.timestamp() * 1000), **overrides))

    def merge(self, existing, readings):
        return bq.merge_sheet_window(existing, readings, self.NOW)

    def keys(self, window):
        return [values[bq.SHEET_KEY_INDEX] for values in window]

    def test_a_row_carries_the_local_clock_three_ways(self) -> None:
        # The full timestamp plots a time series, the date groups by day, and
        # the clock time alone is what overlays one day on another.
        values = bq.sheet_row(row())
        self.assertEqual(len(values), len(bq.SHEET_HEADER))
        self.assertEqual(values[0], "2026-09-04 17:33:34")
        self.assertEqual(values[1], "2026-09-04")
        self.assertEqual(values[2], "17:33:34")
        self.assertEqual(values[bq.SHEET_KEY_INDEX], ENTRY["date"])

    def test_the_newest_reading_is_the_first_row(self) -> None:
        window = self.merge([], [self.reading(60), self.reading(5), self.reading(30)])
        self.assertEqual(self.keys(window), sorted(self.keys(window), reverse=True))
        self.assertEqual(window[0][3], ENTRY["sgv"])

    def test_a_repeated_reading_updates_its_row_instead_of_adding_one(self) -> None:
        # xDrip resends, and revises a value after a calibration. Both must
        # land on the row that instant already has.
        first = self.merge([], [self.reading(10)])
        again = self.merge(first, [self.reading(10, sgv=140, direction="SingleUp")])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0][3], 140)
        self.assertEqual(again[0][5], "SingleUp")

    def test_readings_older_than_the_window_are_dropped(self) -> None:
        inside = self.reading(60 * 23)
        outside = self.reading(60 * 25)
        window = self.merge([bq.sheet_row(outside)], [inside])
        self.assertEqual(self.keys(window), [bq.sheet_row(inside)[bq.SHEET_KEY_INDEX]])

    def test_the_cutoff_follows_the_clock_not_the_newest_reading(self) -> None:
        # A backlog flushed after days offline must not drag the window back
        # with it: the sheet shows the recent day, and BigQuery has the rest.
        window = self.merge([], [self.reading(60 * 50), self.reading(60 * 49)])
        self.assertEqual(window, [])

    def test_the_boundary_is_inclusive_to_the_millisecond(self) -> None:
        cutoff_ms = int((self.NOW - timedelta(hours=24)).timestamp() * 1000)
        on_the_boundary = row(dict(ENTRY, date=cutoff_ms))
        just_outside = row(dict(ENTRY, date=cutoff_ms - 1))
        self.assertEqual(len(self.merge([], [on_the_boundary])), 1)
        self.assertEqual(self.merge([], [just_outside]), [])

    def test_unparseable_existing_rows_are_dropped_rather_than_trusted(self) -> None:
        # Rows come back from a spreadsheet as whatever is in the cells, which
        # someone may have edited by hand.
        existing = [["", "", ""], ["2026-09-05 11:00:00", "x", "y", 100, 0, "Flat", "d", "oops"]]
        window = self.merge(existing, [self.reading(5)])
        self.assertEqual(len(window), 1)

    def test_existing_rows_survive_a_merge_that_adds_nothing_to_them(self) -> None:
        existing = self.merge([], [self.reading(20), self.reading(15)])
        window = self.merge(existing, [self.reading(5)])
        self.assertEqual(len(window), 3)

    def test_the_written_range_covers_the_header_and_every_row(self) -> None:
        self.assertEqual(bq.sheet_range("recent", 3), "recent!A1:H4")
        # An empty window still rewrites the header, so the tab never looks
        # like it was never set up.
        self.assertEqual(bq.sheet_range("recent", 0), "recent!A1:H1")

    def test_a_shrinking_window_clears_what_it_no_longer_covers(self) -> None:
        # Writing a block only overwrites what it covers, so without this the
        # tail of the previous write stays behind looking like live data.
        self.assertEqual(bq.stale_range("recent", 2, 5), "recent!A4:H6")
        self.assertIsNone(bq.stale_range("recent", 5, 5))
        self.assertIsNone(bq.stale_range("recent", 7, 5))


class MirrorReportingTests(unittest.TestCase):
    """What the handler says about the spreadsheet, and what it refuses to do.

    The sheet cannot be read from an operator's machine, so these headers are
    the only visibility into it. They are also the guard on the rule that
    matters most here: a derived destination must never cost a reading.
    """

    def handle(self, mirror):
        stored: list = []
        handler = bq.Handler(
            secret_payload={"salt": "", "digest": ""},
            appender=stored.extend,
            mirror=mirror,
            entries_table="p.cgm.entries",
        )
        request = bq.core.Request(
            method="POST", path="/api/v1/entries", body=json.dumps([ENTRY]).encode()
        )
        return handler._store(request), stored

    def test_the_window_size_is_reported(self) -> None:
        response, _ = self.handle(lambda rows: ("updated", 7))
        self.assertEqual(response.headers["x-xdrip2gcp-sheet"], "updated")
        self.assertEqual(response.headers["x-xdrip2gcp-sheet-rows"], "7")

    def test_no_mirror_configured_is_reported_as_skipped(self) -> None:
        response, _ = self.handle(None)
        self.assertEqual(response.headers["x-xdrip2gcp-sheet"], "skipped")
        self.assertEqual(response.headers["x-xdrip2gcp-sheet-rows"], "0")

    def test_a_failed_mirror_still_stores_the_reading_and_answers_200(self) -> None:
        # The spreadsheet is derived from a row that is already in BigQuery.
        # Failing the request would make the phone retry a reading it has
        # already delivered, to fix something a retry cannot fix.
        response, stored = self.handle(lambda rows: ("failed", 0))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["x-xdrip2gcp-sheet"], "failed")
        self.assertEqual(len(stored), 1)


if __name__ == "__main__":
    unittest.main()
