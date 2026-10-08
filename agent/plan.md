# Plan to Move xDrip Data to GCP

We're going to move data from [xDrip](https://navid200.github.io/xDrip/) into a GCP project. This will follow several steps to get everything working and tested. Our end goal is to get data into [BigQuery](https://cloud.google.com/bigquery) and use [Looker Studio / Data Studio](https://cloud.google.com/data-studio). Our plan is to use the [Nightscout](https://nightscout.github.io/) API capabilities in xDrip to send data to a GCP function, using the data from there.

This plan will be broken into stages, some performed by the developer and some performed by an agent. This will be:

1. Set up local dependencies (mainly for testing) and a GCP project.
2. Write to a GCP storage bucket using the `gcloud` cli.
3. Make a GCP function that can write to the bucket and use the `gcloud` cli to access it.
4. Send data from xDrip through that GCP function and into the bucket from a phone.
5. After the test pipeline works, form a production pipeline for BigQuery data.
6. Mirror the recent day into Google Sheets, so dashboards can read it without querying BigQuery.

## Stage 1: GCP Project Setup and Local Dependencies (Developer) ✓ Done

This reflects my local setup. Your setup may vary.

### 1.1 Install local Python and console tools

* A local version of Python at version 3.10 or up is needed. (Needed for `gcloud` ro run.) My system Python is older. There is a `conda` environment in `resources/` named `environment.yml`. (Note: I use conda forge instead of the default conda repos to avoid license issues.)
    * Example: `conda create --file environment.yml && conda activate xdrip2gcp`
* Install the `gcloud` cli tool.
    * Example: `brew install --cask gcloud-cli`
* You must point `gcloud` at your Python 3.10+ whenever it is executed, if your system Python doesn't match that.
    * Example: `export CLOUDSDK_PYTHON="$CONDA_PREFIX/bin/python"`
* Now, `gcloud version` should run without warnings.

### 1.2 GCP Project Setup

* Set up a GCP project. I used the [console](https://console.cloud.google.com/) and named my project `xdrip2gcp`.
* Link a billing account so we can make storage buckets. (Inside your project in the console, use the billing tab. 5GB is free for buckets.)

### 1.3 Auth `gcloud` and make sure it can see the project

Auth:
```bash
gcloud auth login
```

Can `gcloud` see your project:
```bash
gcloud projects list
```

Use the `PROJECT_ID` (in this case `xdrip2gcp`) to set the current default project:
```
gcloud config set project xdrip2gcp
```

## Stage 2: Write to the test bucket (Agent) ✓ Done

Everything in this stage talks to GCP through the `gcloud` cli (per the plan above), so there
are no new Python dependencies: the `xdrip2gcp` conda environment as it stands is enough.
Config parsing uses `tomllib`, which is in the standard library, and the tests use `unittest`.

### 2.1 Layout

```
resources/config.toml         shared defaults, committed
resources/config.local.toml   per-machine overrides, git-ignored, generated on first run
src/xdrip2gcp/config.py       three-layer config loading, validation, bucket-name resolution
src/xdrip2gcp/gcloud.py       subprocess wrapper around the gcloud cli
src/xdrip2gcp/bucket.py       idempotent bucket and object operations
src/xdrip2gcp/testdata.py     seeded generation of the generic test payloads
src/create_bucket.py          entry point: create the bucket
src/write_test_data.py        entry point: write, verify or clean the test data
test/                         unittest suite (offline tests plus live GCP tests)
```

### 2.2 Configuration

Nothing is hardcoded; project ID, bucket name, location, storage class, object prefixes and
test-data settings all live in `resources/config.toml`. Three layers are merged, each
overriding the previous one:

1. `resources/config.toml` — shared, committed defaults.
2. `resources/config.local.toml` — per-machine, git-ignored.
3. `XDRIP2GCP_*` environment variables (see `ENV_OVERRIDES` in `config.py`).

This layering exists so the repo can be shared: someone else clones it, points
`[project].id` and `[bucket].location` at their own setup in the local file (or via env vars),
and never edits the committed defaults.

The loader also resolves `CLOUDSDK_PYTHON` itself and passes it into every `gcloud` call, so
the scripts and tests work regardless of whether the shell happens to have it exported the way
Stage 1.1 describes.

### 2.3 Bucket naming

The bucket is `xdrip2gcp-test-<suffix>`, where the suffix is six random hex characters generated
on first run. Three reasons it is not plain `xdrip2gcp_test`:

* Bucket names share one global namespace across all of GCP, so an unsuffixed name is likely
  to collide once this repo is shared.
* Google's docs advise against putting project names in bucket names, since the namespace is
  public and anyone can probe for a name to learn the project exists. A random suffix makes
  the name unguessable.
* Underscores are legal in bucket names but break DNS-style addressing (no `CNAME`, no
  virtual-hosted-style URLs), so `config.py` rejects them along with dots and uppercase.

The suffix is generated once and written to `resources/config.local.toml`, then reused forever
after. That is what makes the bucket name stable across runs rather than creating a new bucket
each time.

### 2.4 Idempotency

Every operation is safe to repeat and reports whether it actually changed anything:

* **Bucket creation** checks for the bucket first, and still treats a `409` from a concurrent
  create as success.
* **Lifecycle rules** are compared against what the bucket already has and only updated on a
  real difference.
* **Uploads** compare the payload's MD5 against the stored object's, so re-uploading identical
  bytes skips the transfer entirely instead of merely overwriting. This works because test-data
  generation is seeded from config and therefore byte-for-byte reproducible.
* **Deletes** tolerate an object that is already gone.
* **A name unavailable in the global namespace** raises `BucketOwnedElsewhereError` with
  instructions to change the suffix, rather than a bare `403`.

Two belt-and-braces settings on the bucket: uniform bucket-level access (no legacy per-object
ACLs) and public access prevention (enforced, so the bucket can never be exposed publicly).
A lifecycle rule deletes objects after `[bucket].lifecycle_age_days` days, which caps what
repeated test runs can cost even if a run crashes and abandons objects.

### 2.5 Running it

```bash
conda activate xdrip2gcp
python src/create_bucket.py              # create bucket + lifecycle; --dry-run to inspect only
python src/write_test_data.py --verify   # write test data and read it back; --clean to remove
python -m unittest discover -s test -t . -v
```

The test suite splits in two. `test_config.py` and `test_testdata.py` are offline and run
anywhere in well under a second. `test_bucket_live.py` drives the real bucket, covering
metadata, no-op re-creation, byte-exact round trips (including binary), listing, and idempotent
uploads and deletes. Live tests write to a unique `test-data/scratch/<random>/` prefix and
delete it afterwards, and they *skip* with an explanation rather than fail when `gcloud` is
missing, unauthenticated, or the bucket has not been created yet — so a fresh clone can run the
suite without a GCP account.

### 2.6 Notes and gotchas found along the way

* **Billing must be enabled on the project.** Bucket creation fails without a linked billing
  account even though this all fits inside the always-free tier (5 GB in a US region). Stage 3's
  Cloud Function will need it too.
* `gcloud storage ls --recursive` interleaves `<dir>/:` header lines with real object paths, so
  `list_objects` filters them out; a naive parse reports a nonexistent object named `test-data/`.
* `gcloud auth login` does **not** create Application Default Credentials. That is fine here
  because we shell out to the cli, but a Python client library in a later stage would need
  `gcloud auth application-default login` as an extra step.

### 2.7 Verified results

* The bucket exists in `us-central1`, `STANDARD` class, uniform access on, public access
  prevention enforced, lifecycle deleting objects after 3 days.
* A second `create_bucket.py` run reports both steps as no-ops.
* `test-data/hello.txt` (92 bytes) and `test-data/sample.json` (500 bytes) write, verify, and
  re-write as no-ops.
* 36 tests pass: 21 offline, 15 live. (Stage 3 grew these totals; see 3.9.)

## Stage 3: Create a test GCP function (Agent) ✓ Done

What this stage built:

* A Python HTTP Cloud Function (2nd gen), deployed from this repo with the `gcloud` cli.
* Nightscout-style endpoints: `POST /api/v1/{entries,treatments,devicestatus}` and
  `GET /api/v1/status`, so a Nightscout client can talk to it unmodified.
* Posted documents are written into the Stage 2 test bucket as newline-delimited JSON, and
  tests read them back out to verify them.
* Authentication with a password we hold locally and GCP only ever sees hashed:
    * A Nightscout client sends `sha1(password)` in an `api-secret` header — that is the
      protocol, and it means the plaintext password never crosses the wire.
    * The function hashes that received value again with salted scrypt and compares it, in
      constant time, against a digest stored in Secret Manager.
    * So the plaintext lives only in `resources/config.local.toml` (git-ignored, and the value
      typed into xDrip), while GCP holds a digest that cannot be replayed against the endpoint
      even if it leaks.
* All of it is testable locally and idempotent: re-running the setup and deploy scripts reports
  no-ops, and re-posting the same readings does not create a duplicate object.

Still no new local dependencies. The deployed function has its own `requirements.txt`
(`functions-framework`, `google-cloud-storage`), but nothing was added to the conda
environment: the offline tests drive stdlib-only code, and the live tests use `urllib`.

### 3.1 Layout

```
src/functions/nightscout/main.py              thin Flask-to-core adapter (deployed)
src/functions/nightscout/nightscout_core.py   stdlib-only request handling (deployed + tested locally)
src/functions/nightscout/requirements.txt     the function's own dependencies
src/xdrip2gcp/provision.py                    APIs, service accounts, IAM
src/xdrip2gcp/secretmanager.py                credential digest storage
src/xdrip2gcp/cloudfunction.py                deployment and the redeploy-skip hash
src/xdrip2gcp/function_source.py              imports the function's core module locally
src/xdrip2gcp/actions.py                      shared changed/no-op result type
src/setup_gcp.py                              entry point: prepare the project
src/deploy_function.py                        entry point: secret + deploy; --show-url, --force
src/show_requests.py                          entry point: recent HTTP requests to the function
src/show_latest.py                            entry point: the newest reading stored (bucket or BigQuery)
src/config_env.py                             entry point: shell exports for the gcloud commands
test/test_nightscout_core.py                  offline: the whole request path
test/test_show_latest.py                      offline: newest-reading selection and formatting
test/test_function_live.py                    live: deployment, HTTPS behaviour, bucket contents
```

### 3.2 Authentication: what Nightscout actually sends

This stage was originally specified as "the request sends a password as the Nightscout API
expects, and the function hashes it". Those two halves need reconciling, because **a Nightscout
client never sends the plaintext password**. Both the Nightscout server implementation and
xDrip's own docs confirm
the client computes `sha1(password)` and sends the 40-character hex digest in an `api-secret`
header. xDrip's `https://password@hostname/api/v1/` setting is hashed on the phone before the
request leaves it.

So the function applies a *second* hash, which is the useful version of the requirement:

1. The plaintext password lives in `resources/config.local.toml` (git-ignored). This is the
   value typed into xDrip.
2. Secret Manager holds a self-describing JSON document:
   `{"algorithm":"scrypt","credential_hash":"sha1","n":...,"r":...,"p":...,"salt":...,"digest":...}`,
   where the digest covers `sha1_hex(password)` with a random salt.
3. The function hashes the received header value with scrypt and compares using
   `hmac.compare_digest` for constant time.

Consequences worth knowing:

* GCP never holds anything replayable. Leaking the stored digest does not let an attacker
  authenticate, since they would still need the SHA-1 preimage.
* Storing the work factors *alongside* the digest means they can be raised later without the
  function having to guess which scheme an older secret version used.
* Nightscout's SHA-1 is unsalted, so a weak password could be reversed from an intercepted
  header via rainbow tables. The password is therefore generated (`secrets.token_urlsafe`)
  rather than chosen, from a URL-safe alphabet so it drops into xDrip's URL without escaping.
* Credentials are accepted from the `api-secret` header, HTTP Basic auth, and a `?secret=` or
  `?token=` query parameter, because xDrip carries the password as URL userinfo and some HTTP
  stacks convert that to Basic auth. A 40-character hex value is treated as a digest; anything
  else is treated as plaintext and hashed first, so `curl -H 'api-secret: <password>'` works
  for hand testing.

### 3.3 Endpoints

Routing matches on the `/api/v1/` marker anywhere in the path, so the same code serves both
URL styles (see 3.5).

| Endpoint | Auth | Behaviour |
| --- | --- | --- |
| `GET /api/v1/status[.json]` | none | Nightscout-shaped health JSON; real Nightscout leaves this open and clients use it as a reachability check |
| `GET /api/v1/experiments/test` | required | authorization check, as Nightscout uploaders use |
| `POST /api/v1/entries[.json]` | required | stores CGM readings |
| `POST /api/v1/treatments[.json]` | required | stores treatments |
| `POST /api/v1/devicestatus[.json]` | required | stores device telemetry |

Anything else is a 404 that lists the supported endpoints; a wrong method is a 405.
Authentication is checked *before* the payload is parsed, so an unauthenticated caller learns
nothing about payload validation. Successful writes return the stored documents the way
Nightscout does, keeping the body protocol-faithful, and put our own metadata in
`x-xdrip2gcp-*` response headers where tests can assert on it.

### 3.4 Storage layout and data-path idempotency

Objects are written as newline-delimited JSON at:

```
cgm-data/collection=entries/dt=2026-09-04/1788564814746-d11ead64a0bf55b8.ndjson
                                          |             |
                                          |             sha256 of the contents
                                          earliest reading's own epoch-ms timestamp
```

* **NDJSON** because BigQuery ingests it natively, and BigQuery is the end goal.
* **Hive-style `collection=`/`dt=` partitioning** so a BigQuery external table can read the
  layout directly with no reshaping later.
* **Content-addressed names**, with keys sorted during serialization so the bytes are
  canonical. xDrip queues readings during an outage and retries, so the same batch can arrive
  twice; the retry resolves to the object already stored rather than a duplicate. A client that
  reorders JSON keys still produces the same object.
* **A timestamp prefix taken from the documents themselves.** A hash alone sorts arbitrarily,
  and since both the console and `gcloud storage ls` list objects by name, a bucket listing was
  unreadable as a timeline — the newest upload could appear anywhere in the list. Prefixing with
  the earliest timestamp the batch carries (`date`, `mills`, `created_at`, `sysTime` or
  `dateString`, in that order, accepting epoch seconds, epoch milliseconds or ISO 8601) fixes
  that without giving up idempotency: the prefix comes from the content, not the clock, so a
  retry still produces the identical name. Fixed-width epoch milliseconds make alphabetical
  order chronological order. Documents carrying no timestamp — xDrip's device status — keep
  hash-only names.
* One edge remains: the `dt=` partition uses the server's receive date, so a retry that crosses
  UTC midnight lands in a different partition and does create a second object. xDrip's queue
  drains in minutes, so this is theoretical rather than practical; deriving the partition from
  the documents too would close it, at the cost of putting backfilled readings in older
  partitions.
* The function uploads with `if_generation_match=0`, meaning "only if absent", so a duplicate
  is rejected by Cloud Storage rather than overwritten and the response reports
  `x-xdrip2gcp-stored: duplicate`. This is also why the runtime identity needs only
  `roles/storage.objectCreator` and holds no delete permission anywhere.

### 3.5 Deployment

`src/setup_gcp.py` enables six APIs and creates two dedicated service accounts. `compute` is
among the APIs because gen2 functions build through Cloud Build, whose default identity is the
Compute Engine default service account — on a fresh project that account does not exist yet,
which produces confusing failures. Both accounts are passed explicitly (`--service-account`,
`--build-service-account`) rather than relying on defaults:

* `xdrip2gcp-fn-build` holds `roles/cloudbuild.builds.builder` on the project.
* `xdrip2gcp-fn-runtime` holds `roles/storage.objectCreator` **on the bucket only** and
  `roles/secretmanager.secretAccessor` **on the one secret only**. It has no project-wide role.

`src/deploy_function.py` generates the password if needed, converges the secret, and deploys.
The function is deployed to `us-central1` to match the bucket, capped at 3 instances with a
60-second timeout, and reachable unauthenticated — necessary for a phone, with the api-secret
as the actual guard.

The endpoint uses the Cloud Run URL (`https://<name>-<hash>-uc.a.run.app`) rather than the
`cloudfunctions.net` alias, so the path is exactly `/api/v1/entries` and xDrip's base URL
format lines up. `python src/deploy_function.py --show-url` prints the ready-made xDrip URL for
Stage 4.

### 3.6 Idempotency

* **APIs, service accounts, IAM bindings and the secret** are all checked before acting; a
  second `setup_gcp.py` or `deploy_function.py` run reports every step as a no-op.
* **Secret versions** are only added when the stored digest fails to verify against the local
  password, or when the scrypt parameters changed. Versions cost money and cannot be edited, so
  re-deploying must not mint one each time.
* **Deployment** is skipped when nothing changed. The function carries a `source-hash` label
  covering the source tree, every deploy setting, and a fingerprint of the stored credential.
  The credential is part of it because secret environment variables resolve at instance
  startup, so rotating the password requires a redeploy to take effect — folding the digest
  into the hash makes that automatic. `--force` overrides. Skipping turns a 90-second rebuild
  into a 9-second check.
* **A fresh service account** is not immediately visible to the IAM policy APIs, so bindings
  retry with a delay rather than failing on a propagation race.

### 3.7 Testing

`nightscout_core.py` has no cloud imports and takes its storage writer as an injected callable,
so the offline tests drive the entire request path against a fake writer that reproduces Cloud
Storage's create-only semantics. That covers routing, all three credential locations, digest
versus plaintext detection, scrypt verify and reject, payload validation and size limits,
canonical serialization, and duplicate detection — with no dependencies and in under a tenth of
a second.

`test_function_live.py` then checks the real thing over HTTPS: that the deployment matches
config and runs as the least-privilege identity, that the stored digest contains no replayable
credential but does verify the local password, that all four credential styles authenticate and
wrong ones get 401 with nothing written, that status codes are right for bad methods, unknown
paths and malformed payloads, and that a posted batch lands in the bucket as NDJSON with the
right content type and re-posting is recognized as a duplicate. It deletes the objects it
creates.

```bash
conda activate xdrip2gcp
python src/setup_gcp.py                      # APIs, service accounts, IAM; --dry-run to inspect
python src/deploy_function.py                # secret + deploy; --force to redeploy anyway
python src/deploy_function.py --show-url     # the endpoint, and the xDrip base URL
python -m unittest discover -s test -t . -v
```

### 3.8 Notes and gotchas found along the way

* **A brand-new project has no service accounts at all** and none of the needed APIs. The
  Compute API in particular has to be on before a gen2 function can build.
* **`python314` is available for gen2 functions**, which matches the conda environment, so the
  offline tests exercise the same language version the deployed function runs.
* Cloud Storage's `if_generation_match=0` precondition turned out to remove the need for delete
  permission entirely, which is stricter than the least-privilege plan started out being.
* `urllib`'s `HTTPError` is itself a response object holding an open socket; not closing it
  makes `ResourceWarning`s appear mid-test-run.

### 3.9 Verified results

* Function `xdrip2gcp-nightscout-test` is ACTIVE in `us-central1` on `python314`, running as
  `xdrip2gcp-fn-runtime`, at the Cloud Run URL reported by
  `python src/deploy_function.py --show-url`.
* First deploy took 88 seconds; a second run skips it in 9 and reports no-ops throughout.
* Nightscout-shaped entries POST successfully, land as NDJSON under
  `cgm-data/collection=entries/dt=.../<epoch-ms>-<hash>.ndjson`, and a repeat POST is reported
  as a duplicate with no second object.
* 144 tests pass: 105 offline (well under a second), 39 live.

## Stage 4: Send data to the test bucket from xDrip on a phone (Developer) ✓ Done

Nothing new gets built here. The Stage 3 function already speaks Nightscout's REST API, so
xDrip needs no special treatment: it is pointed at our endpoint exactly as it would be pointed
at a real Nightscout site.

### 4.1 Set up a shell for the commands below

Two of this project's names are specific to whoever set it up: the bucket's random suffix and
the function's generated hostname. Neither belongs in a checked-in document, so the commands
here read them from the configuration instead:

```bash
conda activate xdrip2gcp
eval "$(python src/config_env.py)"
```

That exports `XDRIP2GCP_BUCKET`, `XDRIP2GCP_BUCKET_URI`, `XDRIP2GCP_FUNCTION`,
`XDRIP2GCP_REGION`, `XDRIP2GCP_URL` and `CLOUDSDK_PYTHON` — the last one covering the manual
export from 1.1. Run `python src/config_env.py` on its own to see the values.

The Nightscout password is deliberately excluded: exported variables are inherited by every
child process and land in shell history, which is the wrong place for a credential.

### 4.2 Get the base URL

```bash
python src/deploy_function.py --show-url
```

The first line is the endpoint. The second is the string to put into xDrip, already in the
format the app expects:

```
https://<password>@<function-host>/api/v1/
```

Both the password and the host are filled in for you; `<function-host>` is the same value as
`XDRIP2GCP_URL` without its `https://`.

The password is 32 URL-safe characters (letters, digits, `-` and `_` only), so it needs no
escaping and can be typed or pasted verbatim. It is the only thing guarding a publicly
reachable endpoint, so treat it like any other password: a password manager's secure note is a
good way to move it to the phone, and it should not be pasted anywhere it persists in
plaintext, such as a chat app or email to yourself.

### 4.3 Confirm the endpoint works before touching the phone

```bash
python -m unittest test.test_function_live
```

That exercises authentication and a real write end to end. If those pass, any failure on the
phone is a configuration problem in xDrip, not a problem with the function.

### 4.4 Configure xDrip

1. `Settings` → `Cloud Upload` → `Nightscout Sync (REST-API)`.
2. Enable the feature with the toggle at the top of that page.
3. Tap `Base URL` and enter the whole string from 4.2, including the trailing `/api/v1/`.
4. Leave anything that *downloads* from Nightscout switched off. The function is write-only by
   design, so following or backfilling from it will not work. In particular, do not also set
   `Settings` → `Hardware Data Source` → `Nightscout Follower`.
5. Uploading treatments and device status is fine to leave on; `/api/v1/treatments` and
   `/api/v1/devicestatus` both exist and are stored in their own partitions.

xDrip accepts several sites in the `Base URL` field separated by spaces. Worth knowing if you
also upload to a real Nightscout: when one site is down, xDrip clears its queue as soon as
*any* site accepts the reading, so the down site ends up with gaps.

### 4.5 Verify data is arriving

xDrip uploads as readings come in, so expect the first object within about five minutes.

In the console: `Cloud Storage` → the bucket named by `$XDRIP2GCP_BUCKET` →
`cgm-data/collection=entries/dt=<today>/`. Each object is one upload batch, newline-delimited
JSON, one reading per line. From the command line:

```bash
gcloud storage ls --recursive "$XDRIP2GCP_BUCKET_URI/cgm-data/**"
```

To read the data back rather than just list it, `show_latest.py` prints the newest reading and
how old it is, which is the quickest way to confirm the phone is still uploading:

```bash
python src/show_latest.py                            # the newest reading
python src/show_latest.py --count 10                 # the last ten
python src/show_latest.py --collection devicestatus  # raw JSON
python src/show_latest.py --collection entries       # raw JSON of bg values
```

```
TIME (LOCAL)         MG/DL  DELTA   DIRECTION   DEVICE
2026-09-04 17:58:34  89     +2.0    Flat        xDrip-DexcomG5

newest reading is 2 minutes old (cgm-data/collection=entries/dt=2026-09-04/1788566314865-<hash>.ndjson)
```

Note: Dexcom G7 readings are being sent by xDrip under the device name `xDrip-DexcomG5`.

It exits 4 when the collection is empty, so it also works as a check in a script. Because
object names carry the reading's timestamp, it downloads only the last few objects of the
newest day rather than scanning the bucket.

Faster than waiting for an object to appear, and the best way to see what the phone is actually
doing:

```bash
python src/show_requests.py                  # recent requests
python src/show_requests.py --minutes 10     # just the last ten minutes
```

```
TIME (UTC)           STATUS  METHOD  PATH
2026-09-04 22:01:53  200     POST    /api/v1/entries
2026-09-04 22:01:56  401     POST    /api/v1/entries
```

* `200` — accepted and written.
* `401` — the password in the base URL does not match; re-check it for a typo or a stale value.
* `404` or `405` — xDrip probed an endpoint the function does not implement. Harmless; uploads
  still work. If it turns out to be noisy, the read endpoints can be added.
* No requests at all — xDrip is not reaching the endpoint. Check that the feature toggle is on
  and the URL ends with `/api/v1/`.

Note that `gcloud functions logs read` is the obvious command here but a poor one: for
second-generation functions it returns request entries with an empty message column. The script
queries the underlying Cloud Run request logs instead.

### 4.6 Things to expect

* **Objects self-delete after three days.** This is the test bucket, and its lifecycle rule
  caps cost. History will not accumulate here; that belongs to a later stage with its own
  bucket and a BigQuery table.
* **Retries do not duplicate.** xDrip queues readings when the endpoint is unreachable and
  uploads them later. Because object names are a hash of their contents, a re-sent batch
  resolves to the object already stored.
* **Rotating the password:** change `[auth] password` in `resources/config.local.toml` (or
  delete the line to have a fresh one generated), run `python src/deploy_function.py` to store
  a new secret version and redeploy, then update the `Base URL` on the phone. The redeploy is
  required because the function reads the secret when an instance starts.
* This is real health data landing in a bucket with public access prevention enforced and
  uniform bucket-level access, reachable only by you and the function's runtime identity.

### 4.7 Troubleshooting

**`URISyntaxException: Illegal character in authority` naming an azurewebsites.net URL.**

```
java.net.URISyntaxException: Illegal character in authority at index 8:
https://yourpassphrase@{YOUR-SITE}.azurewebsites.net/api/v1/
```

That URL is xDrip's built-in placeholder, not anything you typed. Java's URI parser rejects the
`{` and `}` in `{YOUR-SITE}`; the reported index 8 is just where the authority component
starts, not where the bad character is. The message means xDrip is trying to upload to the
example value, so either the `Base URL` field was never saved, or — because xDrip accepts
several sites in that field separated by spaces and tries each one — the field holds the
placeholder *alongside* the real URL. In the second case the error recurs on every upload cycle
while uploads still succeed. Fix: open `Base URL` and make sure it contains nothing but the one
URL from 4.2.

**Errors from `doRESTtreatmentDownload`.** Anything in a stack trace that mentions downloading
means a download option is still enabled. The function is write-only, so those calls will fail
or return 404 even once the URL is valid. Turn the download options off; uploads are unaffected.

**Uploads appear to have stopped part-way through the day.** Both the console and
`gcloud storage ls` sort objects by name. Entries now carry an epoch-millisecond prefix so that
order is chronological, but `devicestatus` objects have hash-only names because xDrip sends no
timestamp with them, so those still appear in arbitrary positions. `python src/show_requests.py`
is the reliable way to see whether traffic is still arriving, and this lists objects by creation
time regardless of naming:

```bash
gcloud storage ls --long --recursive "$XDRIP2GCP_BUCKET_URI/cgm-data/**" | sort -k2
```

### 4.8 Verified results

Readings from a Dexcom G5 via a Pixel 9 Pro arrived every five minutes and were stored:

```
cgm-data/collection=entries/dt=2026-09-04/1788564814746-d11ead64a0bf55b8.ndjson
{"date":1788564814746,"dateString":"2026-09-04T17:33:34.746-0600","delta":0,
 "device":"xDrip-DexcomG5","direction":"Flat","filtered":0,"noise":1,"rssi":100,
 "sgv":81,"sysTime":"2026-09-04T17:33:34.746-0600","type":"sgv","unfiltered":0}
```

The object's name prefix, `1788564814746`, is the reading's own `date`, so the listing reads in
time order.

Batch sizes vary: one upload carried two readings, the next carried one, which is why object
names are per batch rather than per reading.

144 tests pass after the naming change: 105 offline, 39 live.

One thing to know for the BigQuery stage: xDrip's `devicestatus` documents look like
`{"device":"Google Pixel 9 Pro","uploader":{"battery":100,"type":"PHONE"}}` and carry **no
timestamp**. Content-addressed naming therefore collapses every identical status post into one
object, so `devicestatus` records when a value *changed* rather than when it was reported. That
is harmless for CGM readings, which each carry their own `date`, but it means device status
cannot be used as a heartbeat. If reporting times matter later, that collection needs the
server's receive time added before it is stored.

## Stage 5: Create a BigQuery datastore using a GCP function (Agent) ✓ Done

What this stage built:

* A second Python HTTP Cloud Function (2nd gen), `xdrip2gcp-nightscout-bq`, deployed from this
  repo with the `gcloud` cli and speaking the same Nightscout REST API as the Stage 3 one. xDrip
  interacts with it identically: same endpoints, same `api-secret` scheme, same password. Only
  the URL differs.
* Readings are appended through the **BigQuery Storage Write API**, on its default stream.
* One table, `entries`, rather than one per collection. `devicestatus` was dropped during design:
  it carries a phone battery level and no timestamp of its own, so it could record when a value
  *changed* but never when it was reported. `entries` each carry their own `date` and need
  nothing assigned by the server.
    * **Partitioned by day** on `reading_date_utc`, and clustered on `reading_date_local`, so a
      query in either frame loads only the days it asks for.
    * **Both clocks are stored, never computed**: `reading_time_utc` alongside
      `reading_time_local` (Mountain wall clock), each with its date, plus `local_offset` and
      `local_zone` so the `MDT`/`MST` in effect is a column rather than an inference.
* **The current reading is published to one Firestore document**, `current/entries`, rather than
  kept in a small BigQuery table. Asking a warehouse for a single value is the one thing it does
  expensively; 5.4 is the measurement that led here. The write is conditional, so a reading that
  arrives late can never drag "now" backwards.
* **Nothing expires.** No dataset default expiration, no partition expiration, and the function's
  identity holds custom roles that cannot delete anything — so the endpoint has no authority to
  rotate its own history out of existence.
* Idempotent and tested. Re-running the setup and deploy scripts reports no-ops, and re-posting a
  reading neither duplicates it in the view nor moves the published value. Verification readings
  are marked `device = 'xdrip2gcp-test'` so they can be told apart from real data. By choice this
  stage has offline tests only, covering reading identity, the row mapping, the daylight-saving
  edge cases and the conditional publish; there is no test dataset, and correctness on GCP was
  confirmed with the earlier stages and `show_latest.py`.
* Naming and configuration follow the existing conventions: `[bigquery]`, `[firestore]` and
  `[function_bq]` sections in `resources/config.toml`, with anything instance-specific staying in
  the git-ignored `resources/config.local.toml`.

Three new dependencies, all of them only inside the deployed function's own `requirements.txt`
(`google-cloud-bigquery-storage` for the appends, `google-cloud-firestore` for the current value,
and `tzdata` because a slim runtime image cannot be assumed to ship a zone database); nothing was
added to the conda environment, and both the offline tests and everything this repo runs locally
are still stdlib-only.

### 5.0 Decisions taken before building

Seven questions were settled first, because each would have been expensive to reverse:

* **A second function, not an extension of the Stage 3 one.** The bucket endpoint stays exactly
  as it was, available whenever the phone should be pointed back at it for testing. The two
  endpoints are not meant to run in parallel: as noted in Stage 4, xDrip clears its upload queue
  as soon as *any* configured site accepts a reading, so two sites means gaps in whichever one
  was down. One at a time.
* **`devicestatus` was dropped.** It carries only a phone battery level and no timestamp, which
  was the awkward part of the original schema. `entries` each carry their own `date`, so nothing
  needs a server-assigned time.
* **Duplicates are collapsed at read time, not prevented at write time.** See 5.2.
* **Partitioned by UTC date, clustered by local date.** Both are stored, so a query in either
  frame prunes.
* **The current value does not live in BigQuery.** It was going to be a two-row table maintained
  by a `MERGE` against itself; 5.4 is what that would have cost and why one Firestore document is
  the better shape. Firestore over the Realtime Database because a Cloud Function writes it
  through an ordinary Google Cloud client with ordinary Google Cloud IAM, where the Realtime
  Database would have added the Firebase Admin SDK and a second access-control model (rules
  deployed with the Firebase CLI) to a project otherwise managed entirely with `gcloud`.
* **`treatments` and anything else is accepted and dropped**, rather than 404'd, so the phone
  neither retries nor fills its log with errors.
* **No live tests and no test dataset for this stage.** Offline tests cover the two things that
  are impossible to eyeball later, and the earlier stages plus `show_latest.py` cover the rest.

### 5.1 Layout

```
src/functions/nightscout_bq/main.py           Flask-to-core adapter, Storage Write API, Firestore (deployed)
src/functions/nightscout_bq/bq_core.py        stdlib-only rows, schema, SQL, current-value rules (deployed + tested)
src/functions/nightscout_bq/requirements.txt  the function's own dependencies
src/xdrip2gcp/bigquery.py                     dataset, table, view, custom role
src/xdrip2gcp/firestore.py                    database, custom role, and a dependency-free document read
src/setup_bigquery.py                         entry point: prepare BigQuery; --dry-run, --drop-table
src/setup_firestore.py                        entry point: prepare Firestore; --dry-run, --show
src/deploy_bq_function.py                     entry point: secret + deploy; --show-url, --force
src/show_latest.py                            entry point: newest reading, --source firestore|bigquery|bucket
src/config_env.py                             entry point: shell exports, now including the Stage 5 names
test/test_bq_core.py                          offline: reading identity, local time, the conditional publish
```

Three commands, in order, and all three converge on no-ops:

```bash
python src/setup_bigquery.py     # dataset, table, view, append-only identity
python src/setup_firestore.py    # the database the current value lives in
python src/deploy_bq_function.py # the function itself
```

The BigQuery function does **not** carry its own copy of `nightscout_core.py`. It imports it, and
`function_source.staged` copies the file in beside it at deploy time, so there is one definition
of the credential scheme no matter which endpoint the phone is pointed at, and no second copy in
the repo to drift. That file's contents are folded into the deploy hash, so editing the shared
module redeploys both functions rather than leaving this one on a stale copy.

### 5.2 Idempotency, when the destination has no way to reject a duplicate

Stages 2 through 4 got idempotency for free: object names were a hash of their own contents, and
`if_generation_match=0` meant Cloud Storage itself refused a second identical write. BigQuery has
no equivalent. Its `PRIMARY KEY` is metadata for the optimizer and is not enforced, and the
Storage Write API's default stream is explicitly at-least-once, so a retry can duplicate a row.

This matters more than it sounds, because duplicates arrive even with no retry at all. Stage 4
recorded xDrip sending overlapping batches: one upload carried two readings, the next carried one
of the same. So the pipeline had to be built to expect them.

The arrangement:

* Every reading gets a **`reading_id`**, the SHA-256 of `"<epoch-ms>|<device>"` truncated to 32
  hex characters. Identity is deliberately the reading's *time and device*, not a hash of the
  whole document, so a value xDrip revises after a calibration resolves to the same row rather
  than becoming a second reading for one instant.
* The raw table is **append-only and never mutated**. Duplicates land in it.
* The **`entries_current` view** returns one row per `reading_id`, keeping the newest `ingest_time`
  of each. Duplicates collapse; a revised value supersedes the original.
* A failed append returns **503**, so xDrip keeps the reading in its own queue and retries. That
  retry is what replaces the bucket as a safety net on this path, and it is safe precisely
  because the view collapses whatever the retry adds.

A batch that contains the same reading twice is still collapsed in Python before anything is
written. Duplicate rows would be harmless in an append-only table, but collapsing them keeps the
row count in the response honest and makes "which of these is newest", the question the current
value depends on, one with a single answer.

### 5.3 Both clocks are stored, not computed

Each row carries `reading_time_utc` (TIMESTAMP), `reading_time_local` (DATETIME, the Mountain wall
clock), `reading_date_utc` and `reading_date_local` (the partition and cluster keys), plus
`local_offset` and `local_zone`. Nothing has to convert at query time, which was the requirement.

The offset and abbreviation are not decoration. On 1 November 2026 the clocks go back at 02:00
MDT, so 01:30 local happens twice, an hour apart. The wall clock alone cannot tell those readings
apart; `local_zone` says `MDT` for the first and `MST` for the second. Converting *from* UTC is
what keeps this unambiguous, and `test/test_bq_core.py` pins both that hour and the hour in March
that never happens at all.

The zone is `[bigquery].timezone` in config rather than hardcoded, validated at load time against
the IANA database, and `tzdata` is in the function's requirements rather than trusting the
runtime image to ship a zone database.

### 5.4 The current value, and why it is not in BigQuery

The requirement was a small, cheap place to read the newest reading from while the day goes on.
The first design was a two-row `entries_latest` table, and pricing it out is what moved it out of
BigQuery altogether.

The obvious implementation — rebuild the small table from the history with `ORDER BY ... LIMIT 2`
— is the wrong shape: `ORDER BY`/`LIMIT` prunes no partitions and the dedupe window function
forces a pass over all of them, so every upload would scan the entire history. At 288 readings a
day and roughly 440 bytes a row that is about 400 GB a month in year one, growing linearly and
crossing the 1 TiB monthly free allowance in year three.

That is avoidable. The function already holds the reading it just wrote, and the only other input
to "the two most recent" is the two rows already there, so a `MERGE` against that table alone does
the job without reading the history: combine its contents with the new rows, rank, keep the top
two, `WHEN NOT MATCHED BY SOURCE THEN DELETE` the rest. Measured rather than estimated, each such
statement bills **10,485,760 bytes — exactly the 10 MiB minimum BigQuery charges per table
referenced — while processing 38 bytes**, or about 86 GB a month, flat forever.

Flat forever, but not free, and that 10 MiB floor is the point. BigQuery bills the same minimum
per table per query whether one row is wanted or a hundred thousand, which makes "what is the
value right now" the one question a warehouse answers badly. Every read by a dashboard or a widget
would pay it too. So the current reading is not stored in BigQuery at all:

* The function publishes it to **one Firestore document**, `current/entries`, overwritten in place.
  At 288 writes a day that is about 1.4% of Firestore's free allowance of 20,000 document writes
  per day; the free tier also covers 50,000 reads a day, which is a reader polling every two
  seconds around the clock. In practice a reader should not poll at all — Native mode supports
  realtime listeners, so it can be pushed each new value.
* The document carries what a reader needs and nothing more: `sgv`, `delta`, `direction`,
  `device`, the reading's UTC timestamp, its epoch milliseconds, and the local wall clock as text
  beside `local_zone` and `local_offset`. Local time is text because Firestore has no `DATETIME`,
  and a naive datetime would be stored as a timestamp and read back as UTC, silently wrong by
  however many hours Denver is behind.
* **The write is conditional.** The newest reading *received* is not the newest reading: xDrip
  resends, and a batch queued during an outage arrives carrying old timestamps. So the publish
  runs in a transaction that compares `reading_epoch_ms` against what is already there and leaves
  a newer value alone. The transaction, rather than a bare compare-and-set, is because two
  uploads can be in flight at once — a retry going out alongside the next reading — and a lost
  update would leave a stale value on display for five minutes. Equal timestamps do overwrite, so
  a replay refreshes rather than being rejected.
* **Failure never fails the request.** The reading is already in BigQuery by then, so the response
  reports what happened in `x-xdrip2gcp-current` — `updated`, `stale` or `failed` — and the next
  upload publishes again five minutes later. There is no repair step to run and no derived table
  that can be left inconsistent.

Removing the `MERGE` had a second effect worth recording: the function no longer runs a query at
all, so its BigQuery role lost `jobs.create` and every read permission. See 5.5.

### 5.5 Permanence, enforced rather than configured

No dataset default expiration, no partition expiration, and nothing the function can reach issues
a drop. The part that needed real thought was IAM: `roles/bigquery.dataEditor`, the obvious grant,
can delete tables, which is exactly the authority a public endpoint should not have. So the
function runs as `xdrip2gcp-bq-runtime` holding two custom roles, neither of which can delete
anything:

```
xdrip2gcpBigQueryWriter   bigquery.tables.get  bigquery.tables.updateData
xdrip2gcpCurrentWriter    datastore.databases.get  datastore.entities.get
                          datastore.entities.create  datastore.entities.update
```

The BigQuery role is two permissions because publishing the current value to Firestore left the
function **append-only**: read the table's schema, append to it, nothing else. No `jobs.create`, so
it cannot run a query; no `tables.getData`, so it cannot read back what it wrote. The Firestore
role can create and overwrite a document but not delete one, and cannot delete or export a
database. (Firestore's permissions are named `datastore.*` for historical reasons.)

Both bindings are project-level rather than scoped to the dataset, which is looser than strictly
necessary, but the permissions are narrow and the project holds nothing but this data.

Two locations are fixed at creation and cannot be changed afterwards: the BigQuery dataset's and
the Firestore database's. Both follow `[bucket].location`, and both setup scripts print what they
found rather than assuming. Firestore additionally allows one free database per project and fixes
its mode at creation, so `setup_firestore.py` reports an existing database instead of trying to
reconcile one — and says so plainly if it finds one in Datastore mode, which cannot serve realtime
listeners.

### 5.6 Schema

One table, `entries`, with typed columns for the fields xDrip sends and a native `JSON` column
holding the document as received:

```
reading_id STRING NOT NULL        reading_time_utc TIMESTAMP NOT NULL
reading_date_utc DATE NOT NULL    reading_time_local DATETIME NOT NULL
reading_date_local DATE NOT NULL  local_offset STRING    local_zone STRING
sgv INT64    delta FLOAT64    direction STRING    device STRING
entry_type STRING    noise INT64    rssi INT64
filtered FLOAT64    unfiltered FLOAT64
date_string STRING    sys_time STRING
ingest_time TIMESTAMP NOT NULL    raw JSON
```

`raw` is the hedge: a field xDrip starts sending tomorrow is captured today, even though it has no
column of its own yet. Absent fields are stored as NULL rather than zero, which is why the
protobuf descriptor the Storage Write API needs is generated as **proto2** — proto3's implicit
presence would turn a missing `sgv` into a reading of 0.

`bq_core.SCHEMA` is the single definition. Both the table DDL and the protobuf descriptor are
generated from it, so the wire format cannot drift from the table, and `setup_bigquery.py` applies
a column added there to the existing table with `ALTER TABLE ADD COLUMN IF NOT EXISTS` rather than
needing a migration.

No SQL carries a value from the phone. The only statements the function issues are the appends,
which go over the Storage Write API as protobuf; the DDL in `setup_bigquery.py` names columns and
tables and nothing else.

### 5.7 Point xDrip at it

The same arrangement as 4.1 and 4.2, for the other endpoint. First a shell that knows the
instance-specific names:

```bash
conda activate xdrip2gcp
eval "$(python src/config_env.py)"
```

Alongside the Stage 3 variables that adds `XDRIP2GCP_BQ_URL`, `XDRIP2GCP_BQ_FUNCTION`,
`XDRIP2GCP_DATASET`, `XDRIP2GCP_ENTRIES`, `XDRIP2GCP_CURRENT` and `XDRIP2GCP_FS_DOCUMENT`, so
nothing below has to name a generated hostname.

Then the connection string:

```bash
python src/deploy_bq_function.py --show-url
```

The first line is the endpoint; the second is what goes into xDrip, already in the format the app
expects:

```
https://<password>@<function-host>/api/v1/
```

Both halves are filled in for you. `<function-host>` is `XDRIP2GCP_BQ_URL` without its
`https://`, and the password is the *same* one the Stage 3 endpoint uses — the two functions share
one Secret Manager credential precisely so that moving the phone between them is only a URL
change. It is still 32 URL-safe characters, so it needs no escaping.

In xDrip, `Settings` → `Cloud Upload` → `Nightscout Sync (REST-API)`, and replace the whole
`Base URL` with the new string, keeping the trailing `/api/v1/`. Everything from 4.4 still
applies, with one difference worth repeating: **replace it rather than appending it.** xDrip
accepts several sites separated by spaces, but it clears its upload queue as soon as *any* of
them accepts a reading, so pointing it at both endpoints gives you gaps in each rather than a
complete copy in both. Run one at a time; the bucket endpoint stays deployed and is there
whenever you want to switch back for testing.

Uploads of `treatments` and `devicestatus` can be left switched on. This endpoint accepts them
with a 200 and stores nothing, which keeps the phone from retrying or logging errors.

To verify, `show_latest.py` reads all three destinations and defaults to the cheapest — the one
Firestore document:

```bash
python src/show_latest.py                             # the current value, one document read
python src/show_latest.py --raw                       # the whole published document
python src/show_latest.py --source bigquery --count 10 # the last ten, from the entries_current view
python src/show_latest.py --source bucket             # the Stage 3 path, unchanged
```

`setup_firestore.py --show` prints the same document with the reading's age, if you would rather
not think about which source you are asking.

`show_requests.py` still answers the "is the phone reaching the endpoint at all" question, but
against the Stage 3 function; for this one, read the logs directly:

```bash
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=$XDRIP2GCP_BQ_FUNCTION" \
  --freshness=10m --limit=20 --format="value(textPayload)"
```

And in the console, `BigQuery` → the dataset named by `$XDRIP2GCP_DATASET`.

### 5.8 Verified results

All three scripts are idempotent; a second run of each reports only no-ops, including
`--drop-table` on a table that is already gone.

The endpoint, probed live: `/status` and `/experiments/test` answer, a wrong secret gets 401, an
entry with no timestamp gets 400 naming the fields it looked for, an unknown path gets 404 listing
what is supported, and `devicestatus` and `treatments` get 200 with `x-xdrip2gcp-stored: ignored`.
A stored reading returns headers reporting the table, the row count, the document path, and what
became of the current value.

The conditional publish, verified directly. A test reading back-dated two hours was accepted and
published; a second one back-dated three hours was appended to the table but left the document
untouched, down to its `updateTime`:

```
two hours old: 200                    three hours old: 200
  x-xdrip2gcp-stored: appended          x-xdrip2gcp-stored: appended
  x-xdrip2gcp-current: updated          x-xdrip2gcp-current: stale
```

Both appends succeeded under the reduced role, which is the practical proof that the function
needs neither `jobs.create` nor any read permission. Five minutes later the phone's own reading
superseded the test value with no intervention, which is the whole argument for not having a
repair step.

Eleven days of real running, 4 to 15 September: **3,045 rows appended, 3,037 distinct readings.**
The eight extra rows are duplicate appends — some from the verification above, the rest xDrip
resending on its own — collapsed by `entries_current` exactly as intended. The current value reads
back four minutes old, from the phone:

```
$ python src/show_latest.py
TIME (LOCAL)         ZONE  MG/DL  DELTA   DIRECTION   DEVICE
2026-09-15 18:12:08  MDT   141    +0.0    Flat        xDrip-DexcomG5

newest reading is 4 minutes old (from current/entries)
```

The dataset now holds `entries` and the `entries_current` view, and nothing else; `entries_latest`
was removed with `setup_bigquery.py --drop-table entries_latest`.

175 tests pass: 136 offline, 39 live.

Two things to know:

* The `python314` runtime was the main risk here, since `google-cloud-bigquery-storage` pulls in
  `protobuf` and `grpcio`, and `google-cloud-firestore` pulls in more of the same. Cloud Build
  resolved them without trouble; no runtime downgrade was needed.
* Eight rows in the permanent table were marked `device = 'xdrip2gcp-test'` as of this stage, one of
  them carrying `sgv = 999` — an impossible value, chosen so that a row written to test the
  timestamp comparison could never be read as data. They can be excluded with
  `WHERE device != 'xdrip2gcp-test'`, or deleted outright — though not for the first while after
  they are written, since rows recently added through the Storage Write API resist DML until the
  streaming buffer flushes. Exclude on the device and not on the value: the real sensor has reported
  into the 460s. `agent/architecture.md` carries the current count.

### 5.9 Run a BigQuery query

Use the BigQuery console in your GCP project, find your way to the `xdrip2gcp` resource, and under `cgm` you should see the created tables. You can now run a query such as

```
SELECT * FROM `xdrip2gcp.cgm.entries_current`
WHERE device != 'xdrip2gcp-test'
ORDER BY reading_date_utc DESC LIMIT 1000
```

Read through `entries_current` rather than `entries`, so that duplicates and superseded values are
collapsed, and exclude the test device so that rows written during verification stay out of the
answer.

## Stage 6: Mirror the last 24 hours into Google Sheets (Agent) ✓ Done

A third destination on the same upload, for one specific reason: **Looker Studio issues a separate
BigQuery query per chart**, and a dashboard with a handful of charts refreshing through the day adds
up against the free terabyte. Looker's Sheets connector does not pay that cost. So the heavy
history stays in BigQuery for the analytical work, and a rolling 24-hour window is mirrored into a
spreadsheet for the recent-day and recent-hour timecharts that get looked at most.

Nothing about BigQuery or Firestore changes. This is purely additive: the same function, on the
same upload, gains a third write.

### 6.0 Decisions taken before building

* **In the upload function, not a separate one.** A Firestore-triggered writer was the first
  instinct, and it would have kept the upload path untouched — but see 6.1: once the window is
  time-based it needs no history, so the isolation buys much less than it costs in moving parts.
* **Strictly the last 24 hours, measured from wall-clock now**, not a fixed row count. A fixed 288
  rows would silently reach further back than a day whenever readings were missed. The consequence
  is that a multi-day backlog flush writes almost nothing to the sheet, which is correct: the sheet
  is a view of the recent day, and the backlog is already in BigQuery.
* **Rows are keyed by reading time.** A resent or revised reading updates its own row rather than
  adding one. This is the same idempotency as the `entries_current` view and the conditional
  Firestore write, achieved the only way a spreadsheet allows.
* **Newest first.** The most recent reading is in row 2, under the header.
* **Nothing is appended.** The window is written as a block, because `append` on an at-least-once
  path accumulates duplicates that nothing would ever collapse.

### 6.1 The sheet is its own state

The idea that makes this cheap: **a 24-hour window is at most ~288 rows, which is small enough to
read back from the sheet on every upload.** So the function does not need the history to rebuild the
window — it reads the sheet it wrote last time, merges the new reading in, drops whatever has aged
out, and writes the block back.

That matters because the function is deliberately history-blind. Its BigQuery role was reduced to
`tables.get` and `tables.updateData` when the current value moved to Firestore (5.5), and rebuilding
a window from `entries_current` would have meant handing back `jobs.create` and read access. Keeping
the window in the sheet avoids that entirely. The append-only identity stays append-only.

Three Sheets calls per upload at most: read the current block, write the merged block, and clear any
trailing rows when the window has shrunk. Against the API's 300 writes per minute per project, at
one upload every five minutes, the quota is irrelevant.

### 6.2 What the sheet holds

Two tabs, with the same columns. `recent` holds the rolling window, and `current` holds the header
and exactly one row — see 6.10 for why a one-row tab is worth having at all.

One header row, then one row per reading, newest first:

```
reading_time_local | reading_date_local | clock_time | sgv | delta | direction | device | reading_epoch_ms
```

`reading_epoch_ms` is the row key, carried in the sheet because the merge needs it and because
sorting on it is exact. `clock_time` is the time of day on its own, which is what makes overlaying
one day on another — the chart a CGM dashboard actually wants — possible in Looker without
expression gymnastics.

Values are written with `valueInputOption=USER_ENTERED` so that Sheets parses the timestamp into a
real datetime and the numbers into real numbers. With `RAW` everything arrives as text and Looker
infers types from strings, which makes date handling unpleasant downstream.

### 6.3 Rotation

The cutoff is `now - 24h`, computed at write time from the same clock the ingest timestamp uses.
Readings older than that are dropped from the block rather than deleted row by row.

One honest consequence: **rotation only happens when a reading arrives.** If the phone is off for
two days, the sheet still holds whatever it held when the last reading landed, until the next upload
rewrites it. The dashboard can filter on `reading_time_local` if that matters; nothing in the sheet
is ever wrong, it is only potentially stale.

### 6.4 Three consequences accepted deliberately

This is on the path a phone depends on every five minutes, so the costs are worth naming:

* **Latency.** Two or three Sheets API calls are added to each upload, a few hundred milliseconds
  to about a second. Against a 60-second timeout, harmless.
* **No transactions.** Sheets has no equivalent of the Firestore transaction used for the current
  value, so two overlapping uploads can both read and one can overwrite the other's row. That costs
  a gap in the dashboard, never a reading, because BigQuery already has it — and it tends to heal
  itself, since xDrip sends overlapping batches anyway (4.6). Capping the function at one instance
  would serialize it, at the cost of queueing.
* **Failure is reported, not raised.** Exactly as with the Firestore publish: the reading is already
  in BigQuery by the time the sheet is touched, so a failure is logged, reported in the
  `x-xdrip2gcp-sheet` response header, and otherwise ignored. The next upload rewrites the window.

With no spreadsheet configured, the write is skipped and the header says so. The function therefore
works unchanged before the sheet exists.

### 6.5 The sheet is not a project resource, and access comes from sharing

Worth stating plainly, because it is the first destination of which it is true. `cgm.entries` and
`current/entries` are **resources inside the GCP project**: the setup scripts create them, the
project owns them, and deleting the project deletes them. The spreadsheet is **a file in a person's
Google Drive**, owned by their personal account. The project's involvement is exactly two things —
`sheets.googleapis.com` enabled so the calls have somewhere to bill quota, and the service account
identity the file is shared with. Nothing here can create, find, or delete it, and it outlives the
project.

That makes this the first piece of the system that cannot be fully provisioned by a script. The
service account gets access the same way a colleague would: **the sheet is shared with its email
address as an Editor.**

No new IAM role is involved. The runtime identity asks the metadata server for a token scoped to
`https://www.googleapis.com/auth/spreadsheets`; the Sheets API then authorizes per file, based on
sharing. No service account key file, no Secret Manager entry, and no app verification, because
there is no user consent flow.

Two things about that token were worth establishing before writing any of it, because both would
have changed the design:

* **A `cloud-platform` token is not enough.** Verified against the live API: it returns 403
  `ACCESS_TOKEN_SCOPE_INSUFFICIENT`. `cloud-platform` is a superset of the Google Cloud scopes, not
  of the Workspace ones, so the scope has to be asked for explicitly.
* **Cloud Run honours a requested scope; Compute Engine does not.** This is the difference between
  this working and needing a whole impersonation apparatus, and it is stated in `google-auth`'s own
  metadata credentials: *"On Compute Engine the metadata server ignores requested scopes. On Cloud
  Run, Flex and App Engine the server honours requested scopes."* A gen2 function runs on Cloud Run,
  so `google.auth.default(scopes=[...])` is sufficient.

The same facts mean the **sheet cannot be read from a laptop** the way `show_latest.py` reads
Firestore. `gcloud auth print-access-token` mints a `cloud-platform` token, and minting a
spreadsheets-scoped one would mean granting the operator `roles/iam.serviceAccountTokenCreator` on
the runtime identity — a standing privilege, for a convenience. Verification therefore happens
through the function itself, which is better evidence anyway: it exercises the real identity on the
real path, and reports the outcome in a response header.

The spreadsheet ID is instance-specific, so it belongs in the git-ignored
`resources/config.local.toml` alongside the bucket suffix and the password. The file's *name* is
never used by anything — only the ID and the tab — so it can be renamed at any time without a
redeploy.

This is also the one place the repo's least-privilege pattern does not hold. Drive has no
append-only role: the narrowest grant that can write a cell is Editor, which can equally clear the
file. There is no way around it, so the containment is to keep the sheet **disposable** — a
dedicated file holding nothing but the generated window, so that the worst case is a file that the
next upload refills.

### 6.6 Layout

```
src/functions/nightscout_bq/bq_core.py        gains: sheet rows, merge-by-time, cutoff, ranges
src/functions/nightscout_bq/main.py           gains: the Sheets read-modify-write adapter
src/functions/nightscout_bq/requirements.txt  gains: google-auth[requests]
src/xdrip2gcp/config.py                        [sheets] -> SheetsConfig
src/xdrip2gcp/cloudfunction.py                 the three XDRIP2GCP_SHEET_* variables
src/deploy_bq_function.py                      prints the address the sheet must be shared with
test/test_bq_core.py                           offline: rotation, in-place update, order, clearing
```

All of the logic lives in `bq_core.py` as pure functions over plain values, with the Sheets calls
injected into the `Handler` as a callable beside `appender` and `publisher`. The parts that bite —
the rotation boundary, a repeated timestamp updating rather than duplicating, ordering, and clearing
trailing rows when the window shrinks — are therefore testable offline, with no spreadsheet.

The three REST calls are made with `google.auth.transport.requests.AuthorizedSession` rather than
the discovery-based `google-api-python-client`. Read a range, write a range, clear a range is not
enough surface to justify the dependency; the only part worth not writing by hand is the token and
its refresh, which is exactly what the session handles. So the only new requirement is
`google-auth[requests]`, which the Google Cloud clients already pull in transitively — pinned
explicitly because it is now depended on directly.

### 6.8 Verify

The function reports what happened on every upload, so the check is the response header rather than
a query:

```
curl -s -o /dev/null -D - -X POST "$XDRIP2GCP_BQ_URL/api/v1/entries" \
  -H "api-secret: $(printf %s "$XDRIP2GCP_PASSWORD" | shasum | cut -d' ' -f1)" \
  -H 'content-type: application/json' --data '[...]' | grep x-xdrip2gcp
```

`x-xdrip2gcp-sheet` says `updated` when the window was rewritten, `skipped` when no spreadsheet is
configured, and `failed` otherwise — in which case the function's log holds the API's own response
body, which distinguishes the two failures that both arrive as a 403: the sheet not being shared
with this identity, and the token lacking the spreadsheets scope.

`x-xdrip2gcp-sheet-rows` reports how many rows the window ended up holding. It exists because the
sheet is the one destination an operator cannot read from their own machine, so without it the
window's size would only be observable by opening the spreadsheet.

### 6.9 Verified results

Setting it up took three tries, and each failure was distinguishable from the log alone, which is
the thing worth recording:

```
x-xdrip2gcp-sheet: failed   403 PERMISSION_DENIED "The caller does not have permission"
                            -> the file had not been shared with the service account
x-xdrip2gcp-sheet: failed   400 INVALID_ARGUMENT "Unable to parse range: recent!A1:H2"
                            -> the tab was still called Sheet1
x-xdrip2gcp-sheet: updated
```

Note what the first failure proves in passing. A missing **scope** fails as 403
`ACCESS_TOKEN_SCOPE_INSUFFICIENT`, which is what a `cloud-platform` token returns; this was 403
`PERMISSION_DENIED`, the per-file kind. So the Cloud Run metadata server really did hand the
function a spreadsheets-scoped token, confirming 6.5 on the live path rather than from a docstring.

The merge was then exercised against the deployed function with a batch of three readings — one
current, one an hour old, one deliberately 30 hours old:

```
--- first send ---          --- same batch again ---
x-xdrip2gcp-sheet: updated  x-xdrip2gcp-sheet: updated
x-xdrip2gcp-sheet-rows: 4   x-xdrip2gcp-sheet-rows: 4
```

Three things fall out of that. The window did not grow on the resend, so merging by reading time
does collapse a repeat rather than duplicating it. The 30-hour-old reading is in `cgm.entries` but
never entered the window, so the cutoff holds on real data. And the count is four rather than three
because real readings from the phone were arriving every five minutes throughout.

One wrinkle met while checking the last point, worth knowing before it wastes someone's afternoon:
the raw table holds **two** identical rows for the resent reading, with one `reading_id` and two
`ingest_time`s. That is the Storage Write API's at-least-once delivery behaving exactly as designed,
and `entries_current` collapses them. Count through the view, never the table.

149 tests pass: 139 offline, including the sheet window and the headers, and 10 skipped or live.

One flaw in the first implementation was found by this process rather than by the tests. The read of
the existing window treated **any** 400 as "the tab is empty", on the guess that a range beyond a
sheet's extent would be rejected. It is not — an empty tab answers 200 with no `values` key — so the
only thing that handling could do was swallow a misnamed tab and report the window as empty. It was
removed. Speculative error handling hid a real misconfiguration, and the write would have surfaced
it a moment later anyway.

### 6.7 Manual setup

Three steps, unavoidable given where a spreadsheet lives:

1. Create a spreadsheet with two tabs, `recent` and `current`. Note its ID from the URL, the part
   between `/d/` and `/edit`.
2. Share it as **Editor** with the runtime identity's email address.
3. Put the ID in `resources/config.local.toml` under `[sheets] spreadsheet_id`.

The tabs have to exist beforehand. Creating them from the function would be possible — the identity
is an Editor, so `batchUpdate` with `addSheet` would work — but it cannot be done from
`deploy_bq_function.py`, where it belongs, because a local `gcloud` token cannot call the Sheets API
at all (6.5). Rather than put provisioning in the upload path and exercise it once a year, a missing
tab is left to fail loudly: the API answers `400 Unable to parse range: <tab>!A1:H2`, which names
the problem precisely.

`sheets.googleapis.com` is added to `[gcp] services`, so the existing setup script enables it, and
`deploy_bq_function.py` prints the address to share with.

Two constraints come from the Looker Studio end rather than from this repo, and both are reasons to
create the file in a particular way:

* **Keep it in My Drive, not a shared drive.** Looker Studio's own troubleshooting notes still say
  it cannot reach files on a Team Drive and that sheets must live in standard Drive folders.
* **The Sheets connector's fastest refresh is every 15 minutes**, where BigQuery's goes down to one.
  So the sheet-backed charts can sit up to fifteen minutes behind a reading that arrived five
  minutes ago — fine for the shape of a day, and the report's manual refresh bypasses the cache when
  the exact latest number matters. The genuinely live value is the Firestore document, which is what
  it is for.

Looker also authorizes as the Google account signed in to Looker Studio, not as the function's
identity, so the two access paths are independent: the service account is an Editor so it can write,
and you reach the same file as its owner. Setting the data source to **Owner's Credentials** means
anyone you later show the dashboard to does not need access to the sheet at all.

### 6.10 A second tab holding only the newest reading

Added after the first week of dashboard work, for a reason that is worth recording because it is not
obvious from the outside: **Looker Studio applies row limits after aggregation**, so "chart only the
most recent reading" is awkward to express. Sorting descending and limiting to one row does not do
it for the chart types that aggregate, and the alternatives — a filter on a computed maximum, or a
blend against a one-row aggregate — are fragile enough to be worth avoiding.

A tab containing exactly one row makes the question disappear. Any chart built on `current` is
already showing the newest reading, with no sort, filter or limit that can be got wrong.

Three properties fall out of the existing design rather than needing new machinery:

* **The row is the first row of the merged window.** The window already holds what the sheet had,
  ordered newest first, so `window[0]` is the newest reading known — not merely the newest of the
  batch. A backlog of old readings therefore leaves the tab alone, exactly as `supersedes()` does
  for Firestore, without a second comparison to write or get wrong.
* **It is always two rows**, header plus one, so unlike the window it can never shrink and needs no
  clearing pass.
* **An empty window writes nothing**, leaving the last known reading on display rather than blanking
  the tab because every reading in a batch happened to be too old to keep.

This overlaps with the Firestore document, which is also "the newest reading" — deliberately. The
difference is who can read it: Looker Studio has a Sheets connector and no Firestore connector, so
the document serves code and this tab serves the dashboard. The cost is one more API call per
upload, against the same 60-second timeout.

Setting `[sheets] current_tab = ""` turns it off and writes only the window.
