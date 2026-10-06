# Getting started

Setting this up from scratch: an empty GCP project at one end, glucose readings arriving in
BigQuery, Firestore and Google Sheets at the other. Expect about half an hour, most of it waiting
for GCP.

This is the short version. [`agent/plan.md`](agent/plan.md) has the long one, stage by stage, and
is where to look when something here is not enough — it is referenced by section below.

## What you need

* Python 3.10 or newer, because that is what the `gcloud` CLI needs to run.
* The [Google Cloud SDK](https://cloud.google.com/sdk/docs/install), which provides both `gcloud`
  and `bq`. On macOS, `brew install --cask gcloud-cli`.
* A GCP project with a billing account linked. Nothing here leaves the free tier, but several of
  these APIs refuse to run at all without a billing account attached.
* An Android phone running [xDrip](https://navid200.github.io/xDrip/) with a working sensor.

## 1. Local setup

There is a conda environment in `resources/environment.yml`, though it is a pinned export from
Apple Silicon macOS, so on another platform you are better off making your own — the only
requirement is the Python version, since everything local is standard library only:

```bash
conda env create -f resources/environment.yml   # or: conda create -n xdrip2gcp python=3.14
conda activate xdrip2gcp
```

Point `gcloud` at that interpreter if your system Python is older, then sign in:

```bash
export CLOUDSDK_PYTHON="$CONDA_PREFIX/bin/python"
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud version    # should be quiet, with no Python warnings
```

## 2. Point the repo at your project

Configuration comes from three layers, each overriding the one before it:

1. **`resources/config.toml`** — shared defaults, committed. Every setting lives here with a
   comment explaining it; read it once and you know what can be changed.
2. **`resources/config.local.toml`** — your machine's overrides, git-ignored, created automatically
   on first run. Two values are *generated* into it rather than configured, so that repeated runs
   stay idempotent: the bucket name suffix (bucket names are globally unique, so this repo's bucket
   cannot collide with anyone else's) and your Nightscout password.
3. **`XDRIP2GCP_*` environment variables** — override both, useful for one-off runs.

The shared default project ID is `xdrip2gcp`. If yours differs, say so once in the local file:

```bash
cat >> resources/config.local.toml <<'EOF'
[project]
id = "YOUR_PROJECT_ID"
EOF
```

The same file is where you would change the region (`[bucket] location`, which BigQuery and
Firestore both follow) or the timezone your readings are stamped in (`[bigquery] timezone`, an IANA
zone name, `America/Denver` by default).

Nothing instance-specific is ever committed. To get the resolved names into your shell — bucket,
dataset, function names, endpoint URLs — use:

```bash
eval "$(python src/config_env.py)"
```

That deliberately omits your Nightscout password, so exported variables are safe to paste into an
issue or a log.

## 3. Create everything

Five commands, in this order. Every one is idempotent: run them again any time and they report
no-ops rather than doing damage.

```bash
python src/create_bucket.py         # the Stage 3 test bucket
python src/setup_gcp.py             # APIs and the two shared service accounts
python src/setup_bigquery.py        # dataset, entries table, dedupe view, append-only identity
python src/setup_firestore.py       # the database holding the current value
python src/deploy_bq_function.py    # the function itself
```

The order matters in two places. `setup_gcp.py` grants the runtime identity a role *on the bucket*,
so the bucket has to exist first; and it creates the build service account that both function
deploys use, so it has to run before any deploy. Every script takes `--dry-run` if you want to see
what it would do first.

`deploy_bq_function.py` generates your Nightscout password on its first run and records it in
`resources/config.local.toml`. GCP only ever sees a salted scrypt digest of it, stored in Secret
Manager.

Optionally, `python src/deploy_function.py` also deploys the original bucket-backed endpoint. It is
worth having: it is a second place to point the phone when you want to test something without
touching real data. See plan.md Stage 3.

## 4. Point xDrip at it

```bash
python src/deploy_bq_function.py --show-url
```

The second line it prints is the connection string, already in the form xDrip wants:

```
https://<password>@<function-host>/api/v1/
```

In xDrip: `Settings` → `Cloud Upload` → `Nightscout Sync (REST-API)`, enable it, and put that
string in `Base URL`, keeping the trailing `/api/v1/`.

**Replace the Base URL rather than adding to it.** xDrip accepts several sites separated by spaces,
but it clears its upload queue as soon as *any* of them accepts a reading, so pointing it at two
endpoints gives you gaps in both rather than a complete copy in each. Uploads of `treatments` and
`devicestatus` can be left switched on; this endpoint accepts them and stores nothing, which keeps
the phone from retrying or logging errors.

Readings arrive every five minutes. Full detail, including troubleshooting for xDrip's more
cryptic log messages, is in plan.md Stage 4.

## 5. Check that it works

The current value, which is one Firestore document read:

```bash
python src/show_latest.py
```

```
TIME (LOCAL)         ZONE  MG/DL  DELTA   DIRECTION   DEVICE
2026-09-15 18:12:08  MDT   141    +0.0    Flat        xDrip-DexcomG5

newest reading is 4 minutes old (from current/entries)
```

If that is empty or stale, work backwards. Is the phone reaching the endpoint at all?

```bash
eval "$(python src/config_env.py)"
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=$XDRIP2GCP_BQ_FUNCTION" \
  --freshness=15m --limit=20 --format="value(textPayload)"
```

A request that stored a reading answers 200 with headers describing what happened —
`x-xdrip2gcp-rows`, `x-xdrip2gcp-stored`, and `x-xdrip2gcp-current`, which reads `updated` when the
current value moved, or `stale` when the reading was older than what was already published.
`x-xdrip2gcp-sheet` reports the spreadsheet mirror from section 7, and says `skipped` until you set
one up.

## 6. Read the data back

**The current value** lives in one Firestore document, `current/entries`, overwritten in place
every five minutes. Its fields are `sgv`, `delta`, `direction`, `device`, `reading_time_utc`,
`reading_epoch_ms`, and the local wall clock as text in `reading_time_local` beside `local_zone`
and `local_offset`. To see the whole thing:

```bash
python src/setup_firestore.py --show
```

**The history** lives in BigQuery. Always read through the `entries_current` view rather than the
`entries` table: appends are at-least-once and the table is append-only, so the view is what
collapses duplicates and superseded values down to one row per reading.

```bash
python src/show_latest.py --source bigquery --count 10
```

```sql
SELECT reading_time_local, sgv, direction
FROM `YOUR_PROJECT.cgm.entries_current`
WHERE reading_date_local >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  AND device != 'xdrip2gcp-test'
ORDER BY reading_time_utc DESC
```

Both timestamps are stored rather than computed, so a query in local time needs no conversion.
Filter on `reading_date_local` or `reading_date_utc` to prune partitions and keep queries cheap.

### Excluding test readings

The table is permanent and append-only, so **everything ever written during setup and verification
is still in it**, including some deliberately absurd values. Expect to meet a reading or two of
`sgv = 999`: an impossible number is how the timestamp logic gets tested without any risk of the row
being mistaken for data. Every such row is marked, so one clause removes all of them:

```sql
WHERE device != 'xdrip2gcp-test'
```

Make that a habit in anything you build on this data — it belongs in your Looker Studio data source
as a filter, not only in one-off queries. Note that `show_latest.py` does *not* apply it, on purpose:
when you are checking whether the pipeline works, a test row is exactly what you want to see.

**Filter on the device, never on the value.** It is tempting to write `WHERE sgv < 400` instead, and
it will quietly throw away real data: a sensor genuinely does report into the 400s, and Dexcom's own
error sentinels sit at 39 and below. `sgv >= 400 OR sgv <= 39` is a reasonable *flag* to show on a
chart, but it is not a way to tell test rows from real ones.

### From your own code

Both are ordinary Google Cloud services, so anything that can authenticate can read them:

```python
from google.cloud import firestore
current = firestore.Client().document("current/entries").get().to_dict()
print(current["sgv"], current["reading_time_local"], current["local_zone"])
```

```python
from google.cloud import bigquery
rows = bigquery.Client().query(
    "SELECT reading_time_local, sgv FROM `YOUR_PROJECT.cgm.entries_current` "
    "ORDER BY reading_time_utc DESC LIMIT 12"
).result()
```

Two things to know before wiring that up. This repo provisions **write** access only — the
function's identity can append to BigQuery and publish the document, nothing more — so a reader
needs its own service account, with `roles/datastore.viewer` for Firestore and
`roles/bigquery.dataViewer` plus `roles/bigquery.jobUser` for BigQuery. And reading Firestore
directly from a browser or mobile app is a different path again: it needs Firebase added to the
project and security rules written, neither of which is set up here. Until then, server-side reads
with a service account are the supported route.

## 7. Optional: mirror the recent day into Google Sheets

Worth doing if you plan to build Looker Studio dashboards. Looker issues a separate BigQuery query
for every chart, which adds up over a day of refreshes; its Sheets connector does not. So the
function can also keep a rolling 24 hours in a spreadsheet, for the charts you look at most.

**Read this before you start, because this destination is not like the other two.** The BigQuery
table and the Firestore document are resources *inside* your GCP project: the scripts create them,
the project owns them, and deleting the project deletes them. A spreadsheet is not. It is an
ordinary file **in your own Google Drive**, owned by your personal Google account, and it would
outlive the project entirely.

The project's only two contributions are that `sheets.googleapis.com` is enabled in it, so the API
calls have somewhere to bill quota, and that the function's service account — a project-owned
identity — is handed access to your file the same way a colleague would be. So this is the one part
that cannot be scripted, and the only part where you grant access in Drive rather than in IAM.

1. Create a blank spreadsheet **in My Drive**, not a shared drive, and rename a tab to `recent`.
   Looker Studio cannot reach files on a shared drive. Name the file whatever you like — nothing in
   this repo reads the name — but pick something that warns you off editing it by hand, because the
   function overwrites the whole tab every five minutes.
2. Copy its ID out of the URL. You do not choose the ID; Google assigns it when the file is created.
   It is the long string between `/d/` and `/edit`:
   `docs.google.com/spreadsheets/d/`**`1a2B3c...xYz`**`/edit`.
3. Share it as **Editor** with the function's identity. `python src/deploy_bq_function.py` prints
   the exact address; it looks like `xdrip2gcp-bq-runtime@YOUR_PROJECT.iam.gserviceaccount.com`.
   Sheets grants access per file through Drive sharing, so no IAM role can substitute for this step.
4. Record the ID and redeploy:

```bash
cat >> resources/config.local.toml <<'EOF'
[sheets]
spreadsheet_id = "YOUR_SPREADSHEET_ID"
EOF

python src/deploy_bq_function.py
```

Within five minutes the `recent` tab fills with the last 24 hours, newest first, under the columns
`reading_time_local`, `reading_date_local`, `clock_time`, `sgv`, `delta`, `direction`, `device` and
`reading_epoch_ms`. The whole block is rewritten on every upload: rows age out past 24 hours, and a
reading xDrip resends or revises updates its own row rather than adding a second one. Anything you
want to keep by hand belongs on a different tab.

Two things to expect. The window only rotates when a reading arrives, so with the phone off the
sheet keeps showing the window as it last stood. And if the mirror fails, the reading is still
stored — `x-xdrip2gcp-sheet` says `failed` and the function's log holds the reason, which is almost
always step 3.

Because the file is yours rather than the project's, you can throw it away and start over at no
cost: delete it, create another, and repeat steps 1 through 4 with the new ID. Nothing is lost,
since every row in it is derived from BigQuery. For the same reason, keep nothing else in this
file — the function clears and rewrites the tab, and Drive has no append-only permission that could
stop it.

### Connecting Looker Studio to it

Create a data source, pick the **Google Sheets** connector, choose the file and the `recent`
worksheet, and leave *Use first row as headers* on. Two settings are worth getting right: set data
freshness to **Every 15 minutes**, which is the fastest the Sheets connector offers, and set data
credentials to **Owner's Credentials** so that showing someone the dashboard does not require
sharing the spreadsheet with them.

Fifteen minutes is the one real cost of reading through Sheets instead of BigQuery, whose freshness
goes down to a minute. It does not affect the shape of the day, a report's manual refresh bypasses
the cache, and the genuinely live value is the Firestore document.

## Running the tests

```bash
python -m unittest discover -s test -t . -v
```

149 offline tests need nothing but Python. A further 39 talk to real GCP and are skipped
automatically when `gcloud` cannot reach your project, so the same command works either way.

Anything the tests write is marked `device = 'xdrip2gcp-test'` and stays in the table forever, since
it is append-only by design. Exclude it as shown in section 6.
