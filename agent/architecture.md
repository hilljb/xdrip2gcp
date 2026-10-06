# Architecture, for whoever picks this up next

Written for an agent joining the project cold. [`plan.md`](plan.md) is the *record* — what was
built, in what order, with the reasoning and measurements behind each decision. This is the *map*:
how the pieces fit, which conventions are load-bearing, and where the sharp edges are.

Read this first, then the plan section for whatever you are about to touch.

## Current state

Stages 1 through 6 are complete and running in production, taking real readings from a phone every
five minutes. Two Cloud Functions are deployed:

| Function | Destination | Purpose |
| --- | --- | --- |
| `xdrip2gcp-nightscout-test` | Cloud Storage bucket | Stage 3. Kept for testing; the phone is not pointed at it. |
| `xdrip2gcp-nightscout-bq` | BigQuery + Firestore + Sheets | Stage 5, extended in Stage 6. The live path. |

Both speak the same Nightscout REST dialect and share one credential, so moving the phone between
them is only a URL change. **They are not meant to run in parallel**: xDrip clears its upload queue
as soon as *any* configured site accepts a reading, so two active sites means gaps in both.

## Conventions that are load-bearing

Break these and things fail in confusing ways rather than obvious ones.

**Everything local reaches GCP through the `gcloud` and `bq` CLIs, never a client library.** This
is not stylistic. Credentials from `gcloud auth login` are user credentials, not Application
Default Credentials, so a local `google.cloud.*` client will not authenticate with them. Wrappers
live in `src/xdrip2gcp/gcloud.py` (`run`, `run_bq`, `bq_query`, `preflight`).

**Local code is standard library only.** `tomllib` for config, `urllib` for HTTP, `unittest` for
tests, `zoneinfo` for timezones. Cloud libraries appear *only* in the deployed function's own
`requirements.txt`. This is why `src/xdrip2gcp/firestore.py` reads a document over the REST API with
a token from `gcloud auth print-access-token` instead of importing the Firestore client.

**Every operation that changes GCP returns an `ActionResult(changed, detail)`** and is idempotent.
Scripts print `[changed]` or `[no-op  ]` per step. The test of correctness is that a second run of
any script reports nothing but no-ops. When adding an operation, make it converge — check the live
state, compare, act only on a difference.

**Test data is marked `device = 'xdrip2gcp-test'`.** The BigQuery table is permanent and append-only
by design, so anything written during verification stays there forever. It can always be excluded
with `WHERE device != 'xdrip2gcp-test'`, and that is the only correct way to exclude it. Some test
rows carry `sgv = 999` so that a row written to exercise timestamp logic can never be mistaken for
data — but **do not filter on the value**: the real sensor has reported into the 460s, so a
`WHERE sgv < 400` cleanup would silently discard genuine readings.

**Instance-specific values are never committed.** No generated bucket suffix, no function hostname,
no password. If you need one in a command, get it from `python src/config_env.py`.

## Layout

```
src/xdrip2gcp/            library: one module per concern, no entry points
  config.py               three-layer loading, validation, generated values
  gcloud.py               subprocess wrappers for gcloud and bq, GcloudError
  provision.py            APIs, service accounts, project-level IAM bindings
  bucket.py               Stage 3 bucket
  bigquery.py             dataset, table, view, custom role, append-only identity
  firestore.py            database, custom role, REST document read
  secretmanager.py        the shared Nightscout credential
  cloudfunction.py        deploy both functions, hash-based skip, env building
  function_source.py      source digests and staging shared modules
  testdata.py             deterministic payloads
  actions.py              ActionResult

src/*.py                  entry points, one per operation, all with --dry-run
src/functions/nightscout/       Stage 3 function; nightscout_core.py lives here
src/functions/nightscout_bq/    Stage 5 function
test/                     unittest; offline tests plus live GCP tests
resources/config.toml     shared defaults, committed
resources/config.local.toml  per-machine, git-ignored, partly generated
```

## The function split: core and adapter

Each function is two files, and the division is the reason the project is testable.

**`nightscout_core.py`** and **`bq_core.py`** are pure: standard library only, no cloud imports, no
I/O. They parse a framework-free `Request`, authenticate, build rows, and return a `Response`. All
side effects arrive as injected callables — `appender`, `publisher` — so the entire request path can
be exercised offline with a fake.

**`main.py`** is the adapter: it translates Flask to that `Request`, supplies real implementations
of the callables, and translates back. It holds every cloud dependency and is deliberately thin
enough that little in it is worth unit testing.

When adding behaviour, put it in the core. If it needs a cloud call, express that as another
injected callable.

`bq_core.py` **imports** `nightscout_core.py` rather than vendoring it; `function_source.staged()`
copies the file in beside it at deploy time. One definition of the credential scheme, no second copy
to drift. That file's bytes are part of the deploy hash, so editing it redeploys both functions.

## Configuration

Three layers, each overriding the last: `resources/config.toml` (committed defaults) →
`resources/config.local.toml` (git-ignored) → `XDRIP2GCP_*` environment variables. Loading, merging
and validation are all in `config.py`; `load_config()` returns a frozen `Config` and raises
`ConfigError` with a message naming the offending key.

Two values are **generated** rather than configured, and persisted to the local file so later runs
resolve identically: the bucket name suffix (bucket names are globally unique) and the Nightscout
password. `load_config(allow_generation=False)` is what read-only scripts use so that merely
inspecting configuration cannot invent a password.

Derived values are properties on `Config` rather than string-formatting at call sites:
`dataset_id`, `entries_table_id`, `current_view_id`, `quoted_table()`, `firestore_document_path`,
`bq_role_name`, `firestore_role_name`, `service_account_email()`, `region_of()`. Add new ones there.

Two locations are immutable once created — the BigQuery dataset's and the Firestore database's —
and both follow `[bucket] location`. Provisioning code must therefore *report* what it finds rather
than try to reconcile it. `ensure_dataset` and `ensure_database` both work that way, and
`ensure_database` says so explicitly if it finds a Datastore-mode database, which cannot serve
realtime listeners and cannot be converted.

## Secrets

The Nightscout password is the only secret, and it lives in exactly one place: the git-ignored
`resources/config.local.toml`. It has never been committed, and `git log -S` confirms it.

The scheme, defined in `nightscout_core.py`, follows Nightscout's own:

1. xDrip sends `sha1_hex(password)` in an `api-secret` header — never the password itself. It also
   accepts the password in a URL's userinfo (`https://password@host/...`), which some HTTP stacks
   convert to basic auth, so the core checks both.
2. The function hashes that received digest a second time with **salted scrypt** and compares
   against a payload in Secret Manager. GCP therefore holds nothing replayable.
3. The secret is mounted as `NIGHTSCOUT_SECRET` and resolved when an instance starts, so rotating
   it requires a redeploy.

Rules to preserve: `config_env.py` exports every resolved name **except** the password, so its
output is safe to paste anywhere. `deploy_*_function.py --show-url` prints the password on purpose —
that is how it gets into the phone — so do not add it to any other output. Never log a received
credential, even a digest.

## Data design

**Identity.** Every reading gets a `reading_id`: `sha256("<epoch-ms>|<device>")` truncated to 32 hex
characters. Identity is the reading's *time and device*, deliberately not a hash of the whole
document, so a value xDrip revises after a calibration resolves to the same reading rather than
becoming a second one for the same instant.

**Duplicates are collapsed at read time, not prevented at write time.** BigQuery cannot reject
them: `PRIMARY KEY` is unenforced metadata and the Storage Write API's default stream is
at-least-once. And duplicates arrive even without retries — xDrip sends overlapping batches. So the
`entries` table is append-only and never mutated, and the **`entries_current` view** returns the
newest ingest of each `reading_id`. Always read the view, never the table.

**Both clocks are stored, never computed.** `reading_time_utc` and `reading_time_local` (Mountain
wall clock), each with its own date column, plus `local_offset` and `local_zone`. Partitioned by
`reading_date_utc`, clustered by `reading_date_local`, so a query in either frame prunes. The offset
and zone are not decoration: on 1 November 2026 the clocks go back and 01:30 local happens twice, so
the wall clock alone is ambiguous while `MDT` versus `MST` is not. `test_bq_core.py` pins both that
hour and the March hour that never happens.

**`bq_core.SCHEMA` is the single schema definition.** The table DDL and the protobuf descriptor are
both generated from it. Adding a column means adding a `Column` there; `setup_bigquery.py` applies it
with `ALTER TABLE ADD COLUMN IF NOT EXISTS`. Columns are only ever added, never dropped or retyped.
There is also a `raw` JSON column holding the document as received, so a field xDrip starts sending
tomorrow is captured today.

**The current value is one Firestore document, not a BigQuery row.** BigQuery bills a 10 MiB minimum
per table referenced per query, so it answers "what is the value right now" expensively no matter
how little you ask for. `current/entries` is overwritten in place every five minutes.

Two details of that write matter. It carries local time as **text**, because Firestore has no
`DATETIME` and a naive datetime would be stored as a timestamp and read back as UTC, silently wrong
by hours. And it is **conditional**: `bq_core.supersedes()` compares `reading_epoch_ms` against what
is published, because the newest reading *received* is not the newest reading — xDrip resends, and a
batch queued during an outage arrives carrying old timestamps. The comparison runs inside a
Firestore transaction, since two uploads can be in flight at once and a lost update would leave a
stale value on display for five minutes. Equal timestamps overwrite, so a replay refreshes.

**The spreadsheet holds its own window.** Stage 6 mirrors the last 24 hours into Google Sheets,
because Looker Studio issues one BigQuery query per chart and its Sheets connector does not. The
window is under 300 rows, which is small enough that the function reads the sheet back on every
upload, merges by reading time, drops what has aged out, and writes the block again. That is why no
BigQuery read permission was restored for it: the sheet is the window's only storage.

**The spreadsheet is not a project resource.** Worth being explicit, because it is the only one:
`cgm.entries` and `current/entries` live inside the GCP project and are created by the setup
scripts, while the spreadsheet is a file in the operator's personal Drive that the project merely
has permission to write to. The project contributes two things and no more — `sheets.googleapis.com`
enabled for quota, and the service account identity that the file is shared with. Nothing in this
repo can create, find, or delete it, and it outlives the project.

Three things about it differ from the other two destinations, and all three are deliberate:

* **Access comes from Drive sharing, not IAM.** The identity is added as an Editor on the file.
  No grant in this project can substitute for that, and Drive offers nothing narrower than Editor,
  so this is the one write path in the system that could destroy what it writes to. The containment
  is that the file is disposable: everything in it is derived from BigQuery.
* **The token needs an explicit scope.** A `cloud-platform` token is rejected by the Sheets API with
  `ACCESS_TOKEN_SCOPE_INSUFFICIENT`. `google.auth.default(scopes=[...spreadsheets])` works because a
  gen2 function runs on Cloud Run, whose metadata server honours requested scopes — on Compute
  Engine it would not. The corollary is that the sheet cannot be read from a laptop, since
  `gcloud auth print-access-token` only mints `cloud-platform`.
* **There is no transaction.** Sheets has no equivalent, so overlapping uploads can clobber a row.
  That costs a gap in a chart, never a reading, and the next upload rewrites the window anyway.

## Identities and IAM

The function runs as `xdrip2gcp-bq-runtime` holding two custom roles. `roles/bigquery.dataEditor`,
the obvious grant, can delete tables — precisely the authority a public endpoint must not have.

```
xdrip2gcpBigQueryWriter   bigquery.tables.get  bigquery.tables.updateData
xdrip2gcpCurrentWriter    datastore.databases.get  datastore.entities.get
                          datastore.entities.create  datastore.entities.update
```

The BigQuery role is two permissions because the function is **append-only**: it can read the
table's schema and append, and that is all. No `jobs.create`, so it cannot run a query; no
`tables.getData`, so it cannot read back what it wrote. The Firestore role can create and overwrite
a document but not delete one, and cannot delete or export a database. If you find yourself wanting
to widen either role, that is a signal to reconsider the change instead.

Permanence is enforced here, not merely configured: no expirations anywhere, and no identity in the
system with authority to drop data. `bigquery.drop_table` exists for the operator, reachable only
through `setup_bigquery.py --drop-table NAME`.

## Deployment

`cloudfunction.deploy` computes a hash over the function's source, its shared modules and its
resolved environment, stores it as a deploy label, and skips the deploy when it matches — **and only
when the function's state is `ACTIVE`**. That second condition exists because a failed deploy still
sets the label, so without it a broken function reports "already deployed" forever. A redeploy over
a failure reports `redeployed (was FAILED)`.

`--force` overrides the skip. Shared modules are staged into a temporary tree by
`function_source.staged()`, so the deployed source is assembled rather than committed in duplicate.

## HTTP contract

Endpoints: `GET /api/v1/status`, `GET /api/v1/experiments/test`, and `POST /api/v1/{entries,
treatments, devicestatus}`.

| Status | When |
| --- | --- |
| 200 | Stored, or accepted-and-ignored for `treatments` and `devicestatus` |
| 400 | A reading with no usable timestamp; the body names the fields it looked for |
| 401 | Missing or wrong credential |
| 404 | Unknown path; the body lists what is supported |
| 405 | Right path, wrong method |
| 413 | Body over `max_request_bytes` |
| 500 | Misconfiguration — missing env var, unreadable secret |
| 503 | Storage failed |

The distinction between 500 and 503 is deliberate. **A storage failure must be a 5xx** so xDrip keeps
the reading in its own upload queue and retries; that queue is the safety net that replaced the
bucket on this path, and the retry is harmless because the view collapses duplicates. Never
downgrade a failed append to a 200.

Collections other than `entries` are accepted with `x-xdrip2gcp-stored: ignored` rather than 404'd,
so the phone neither retries nor fills its log with errors.

Response headers on a stored reading: `x-xdrip2gcp-table`, `-rows`, `-documents`, `-stored`,
`-reading-id`, `-current` (`updated`, `stale`, `failed` or `skipped`), `-current-path`, and
`-sheet` (`updated`, `skipped` or `failed`). These are how the live tests and manual probes assert
behaviour; keep them accurate.

Note what is *not* a 5xx: neither the Firestore publish nor the Sheets mirror can fail the request.
Both are derived from a reading that is already in BigQuery, and both are rewritten by the next
upload, so they report in a header and the response stays a 200.

## Gotchas that cost real time

* **The `bq` CLI writes errors to stdout**, not stderr. Any "does this exist" check must inspect both
  streams — see `bigquery._reports_missing`.
* **The protobuf descriptor must be proto2.** proto3's implicit presence turns an absent `sgv` into a
  reading of 0 instead of NULL.
* **BigQuery DDL wants `NOT NULL`**, not the API's `REQUIRED`/`NULLABLE`. `Column` exposes both
  spellings: `.ddl` for statements, `.mode` for comparing against live metadata.
* **`ROWS` is a reserved word**, so `SELECT COUNT(*) AS rows` is a syntax error.
* **proto-plus messages have no `HasField`.** Check `if response.error.code != 0`.
* **A Cloud Function's generated hostname is not derived from anything**, so it must be looked up
  with `gcloud functions describe`, never constructed.
* Rows written through the Storage Write API **resist DML** until the streaming buffer flushes, so a
  just-written test row cannot immediately be deleted.

## Verifying a change

```bash
python -m unittest discover -s test -t . -v   # 149 offline, 39 live (skipped without gcloud)
python src/setup_bigquery.py                  # must report only no-ops
python src/setup_firestore.py                 # must report only no-ops
python src/deploy_bq_function.py              # deploys only if something really changed
python src/show_latest.py                     # the current value, from the phone
python src/show_latest.py --source bigquery --count 5
```

A change is not finished until the setup scripts converge on no-ops and a real reading has landed
in every destination. The spreadsheet is the one that cannot be checked from here, for the scope
reason above; probe the endpoint and read `x-xdrip2gcp-sheet` instead.

## Known gaps

Not bugs — decisions deferred, and the obvious next work:

* **No reader identity.** Only write access is provisioned. Anything consuming this data needs its
  own service account with `roles/datastore.viewer` and `roles/bigquery.dataViewer` plus
  `roles/bigquery.jobUser`.
* **No client-side Firestore access.** Reading the document from a browser or phone app would need
  Firebase added to the project and security rules written. Server-side reads work today.
* **No read endpoint on the function.** A `GET /api/v1/current` returning the document would let a
  client with the existing password fetch the value without any Google credentials at all — the
  simplest path for a dumb device or a shell script.
* **No live tests for the Stage 5 path**, by choice. Offline tests cover reading identity, the row
  mapping, the daylight-saving edges and the conditional publish; correctness on GCP was confirmed
  by probing the deployed endpoint directly.
* **The spreadsheet rotates only when a reading arrives.** If the phone is off for a day, the sheet
  keeps showing the window as it was at the last upload. A scheduled rewrite would fix it; nothing
  in the sheet is ever wrong, only potentially stale.
* **Overlapping uploads can clobber a sheet row**, since Sheets offers no transaction. Capping the
  function at one instance would serialize it if this ever shows up in practice.
* **Twelve test rows** are permanent in `cgm.entries`, marked `device = 'xdrip2gcp-test'`. Two
  carry `sgv = 999` and a reading time well before their ingest time, from verifying that a late
  reading cannot move the current value backwards (Stage 5) and that the 24-hour sheet cutoff
  refuses an old one (Stage 6). The device filter excludes them like the rest.
