"""Helpers for moving xDrip data into GCP.

Stage 2 scope: load configuration, drive the `gcloud` CLI, and idempotently
create and populate a Cloud Storage test bucket.
"""

__all__ = ["bucket", "config", "gcloud", "testdata"]
