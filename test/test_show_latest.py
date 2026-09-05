"""Offline tests for the latest-reading readback. These make no network calls.

The selection logic is what matters here: which objects get downloaded, and in
what order the documents come back. Storage is a dict, so the tests can also
assert that reading the newest reading does not read the whole bucket.
"""

from __future__ import annotations

import json
import unittest

import show_latest
from xdrip2gcp.function_source import core_module

core = core_module()

DAY = "cgm-data/collection=entries/dt=2026-09-04"
PRIOR_DAY = "cgm-data/collection=entries/dt=2026-09-03"


def entry(timestamp_ms: int, sgv: int) -> dict:
    return {"date": timestamp_ms, "sgv": sgv, "type": "sgv"}


def ndjson(*documents: dict) -> bytes:
    return ("\n".join(json.dumps(document) for document in documents) + "\n").encode("utf-8")


def batch_path(day: str, timestamp_ms: int, name: str = "abcdef0123456789") -> str:
    return f"{day}/{timestamp_ms:013d}-{name}.ndjson"


class FakeBucket:
    """Object bytes keyed by path, recording what was actually fetched."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.fetched: list[str] = []

    def fetch(self, path: str) -> bytes:
        self.fetched.append(path)
        return self.objects[path]

    @property
    def paths(self) -> list[str]:
        return sorted(self.objects)


def latest(store: FakeBucket, count: int = 1) -> list[tuple[dict, str]]:
    return show_latest.latest_documents(
        store.paths, store.fetch, count, core.document_timestamp_ms
    )


class PathOrderTests(unittest.TestCase):
    def test_extracts_partition_day(self) -> None:
        self.assertEqual(show_latest.partition_day(batch_path(DAY, 1)), "2026-09-04")

    def test_missing_partition_day_is_empty(self) -> None:
        self.assertEqual(show_latest.partition_day("cgm-data/loose.ndjson"), "")

    def test_sorts_by_timestamp_prefix_not_string_order(self) -> None:
        # A shorter timestamp would sort first as a string; both are padded to
        # 13 digits precisely so that cannot happen.
        early = batch_path(DAY, 999_999_999_999)
        late = batch_path(DAY, 1_788_566_314_865)
        self.assertEqual(sorted([late, early], key=show_latest.sort_key), [early, late])

    def test_unprefixed_names_sort_first(self) -> None:
        hashed = f"{DAY}/ec078243f1b2c45a.ndjson"
        prefixed = batch_path(DAY, 1_788_566_314_865)
        self.assertEqual(sorted([prefixed, hashed], key=show_latest.sort_key), [hashed, prefixed])

    def test_groups_days_newest_first(self) -> None:
        grouped = show_latest.partitions([batch_path(PRIOR_DAY, 1), batch_path(DAY, 2)])
        self.assertEqual([show_latest.partition_day(day[0]) for day in grouped],
                         ["2026-09-04", "2026-09-03"])


class LatestDocumentTests(unittest.TestCase):
    def test_returns_newest_reading(self) -> None:
        store = FakeBucket({
            batch_path(DAY, 1_000_000_000_000): ndjson(entry(1_000_000_000_000, 80)),
            batch_path(DAY, 1_000_000_300_000): ndjson(entry(1_000_000_300_000, 95)),
        })
        documents = latest(store)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0][0]["sgv"], 95)

    def test_orders_a_multi_reading_batch(self) -> None:
        older, newer = 1_000_000_000_000, 1_000_000_300_000
        store = FakeBucket({batch_path(DAY, older): ndjson(entry(older, 80), entry(newer, 95))})
        self.assertEqual([document["sgv"] for document, _ in latest(store, count=2)], [95, 80])

    def test_finds_a_reading_hidden_in_an_earlier_named_batch(self) -> None:
        # A batch is named for its earliest reading, so this batch sorts before
        # the other one while holding the newest reading of the two.
        straddling = batch_path(DAY, 1_000_000_000_000)
        store = FakeBucket({
            straddling: ndjson(entry(1_000_000_000_000, 80), entry(1_000_000_600_000, 99)),
            batch_path(DAY, 1_000_000_300_000): ndjson(entry(1_000_000_300_000, 90)),
        })
        document, path = latest(store)[0]
        self.assertEqual(document["sgv"], 99)
        self.assertEqual(path, straddling)

    def test_does_not_download_the_whole_day(self) -> None:
        objects = {
            batch_path(DAY, 1_000_000_000_000 + index * 300_000): ndjson(
                entry(1_000_000_000_000 + index * 300_000, 80 + index)
            )
            for index in range(50)
        }
        store = FakeBucket(objects)
        self.assertEqual(latest(store)[0][0]["sgv"], 129)
        self.assertLessEqual(len(store.fetched), 1 + show_latest.LOOKBACK)

    def test_falls_back_to_the_previous_day(self) -> None:
        store = FakeBucket({
            batch_path(PRIOR_DAY, 999_913_600_000): ndjson(entry(999_913_600_000, 70)),
            batch_path(DAY, 1_000_000_000_000): ndjson(entry(1_000_000_000_000, 80)),
        })
        self.assertEqual([document["sgv"] for document, _ in latest(store, count=2)], [80, 70])

    def test_reads_every_object_when_names_carry_no_timestamp(self) -> None:
        day = "cgm-data/collection=devicestatus/dt=2026-09-04"
        store = FakeBucket({
            f"{day}/aaaa.ndjson": ndjson({"device": "phone", "created_at": "2026-09-04T10:00:00Z"}),
            f"{day}/bbbb.ndjson": ndjson({"device": "phone", "created_at": "2026-09-04T18:00:00Z"}),
        })
        document, _ = latest(store)[0]
        self.assertEqual(document["created_at"], "2026-09-04T18:00:00Z")
        self.assertEqual(len(store.fetched), 2)

    def test_tolerates_documents_without_timestamps(self) -> None:
        day = "cgm-data/collection=devicestatus/dt=2026-09-04"
        store = FakeBucket({f"{day}/aaaa.ndjson": ndjson({"device": "phone"})})
        self.assertEqual(latest(store)[0][0], {"device": "phone"})


class FormattingTests(unittest.TestCase):
    def test_ages(self) -> None:
        self.assertEqual(show_latest.human_age(12), "12 seconds old")
        self.assertEqual(show_latest.human_age(300), "5 minutes old")
        self.assertEqual(show_latest.human_age(7200), "2 hours old")
        self.assertEqual(show_latest.human_age(259200), "3 days old")

    def test_clock_skew_is_labelled_not_negated(self) -> None:
        self.assertEqual(show_latest.human_age(-5), "in the future")

    def test_deltas_carry_a_sign(self) -> None:
        self.assertEqual(show_latest.format_delta(2), "+2.0")
        self.assertEqual(show_latest.format_delta(-1.25), "-1.2")
        self.assertEqual(show_latest.format_delta(None), "-")

    def test_local_time_of_unknown_timestamp(self) -> None:
        self.assertEqual(show_latest.local_time(None), "unknown")


if __name__ == "__main__":
    unittest.main()
