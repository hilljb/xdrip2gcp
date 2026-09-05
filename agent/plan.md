# Plan to Move xDrip Data to GCP

We're going to move data from [xDrip](https://navid200.github.io/xDrip/) into a GCP project. This will follow several steps to get everything working and tested. Our end goal is to get data into [BigQuery](https://cloud.google.com/bigquery) and use [Looker Studio / Data Studio](https://cloud.google.com/data-studio). Our plan is to use the [Nightscout](https://nightscout.github.io/) API capabilities in xDrip to send data to a GCP function, using the data from there.

This plan will be broken into stages, some performed by the developer and some performed by an agent. This will be:

1. Set up local dependencies (mainly for testing) and a GCP project.
2. Write to a GCP storage bucket using the `gcloud` cli.
3. Make a GCP function that can write to the bucket and use the `gcloud` cli to access it.
4. Send data from xDrip through that GCP function and into the bucket from a phone.
5. After the test pipeline works, form a production pipeline for BigQuery data.

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
* A small `entries_latest` table holding **only the two most recent readings**, maintained by one
  `MERGE` per upload that reads nothing but itself. Measured at exactly 10 MiB billed per upload,
  flat forever, for the reasons in 5.4.
* **Nothing expires.** No dataset default expiration, no partition expiration, and the function's
  identity holds a custom role that cannot delete a table — so the endpoint has no authority to
  rotate its own history out of existence.
* Idempotent and tested. Re-running the setup and deploy scripts reports no-ops, and re-posting a
  reading neither duplicates it in the view nor changes `entries_latest`. Verification readings
  are marked `device = 'xdrip2gcp-test'` so they can be told apart from real data. By choice this
  stage has offline tests only, covering reading identity, the row mapping, the daylight-saving
  edge cases and the generated SQL; there is no test dataset, and correctness on GCP was
  confirmed with the earlier stages and `show_latest.py`.
* Naming and configuration follow the existing conventions: a `[bigquery]` and a `[function_bq]`
  section in `resources/config.toml`, with anything instance-specific staying in the git-ignored
  `resources/config.local.toml`.

Three new dependencies, all of them only inside the deployed function's own `requirements.txt`
(`google-cloud-bigquery-storage` for the appends, `google-cloud-bigquery` for the `MERGE`, and
`tzdata` because a slim runtime image cannot be assumed to ship a zone database); nothing was
added to the conda environment, and the offline tests still drive stdlib-only code.

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
* **The two-row table is maintained by a MERGE against itself.** See 5.4, which is mostly about
  what this costs.
* **`treatments` and anything else is accepted and dropped**, rather than 404'd, so the phone
  neither retries nor fills its log with errors.
* **No live tests and no test dataset for this stage.** Offline tests cover the two things that
  are impossible to eyeball later, and the earlier stages plus `show_latest.py` cover the rest.

### 5.1 Layout

```
src/functions/nightscout_bq/main.py           Flask-to-core adapter, Storage Write API, MERGE (deployed)
src/functions/nightscout_bq/bq_core.py        stdlib-only rows, schema and SQL (deployed + tested locally)
src/functions/nightscout_bq/requirements.txt  the function's own dependencies
src/xdrip2gcp/bigquery.py                     dataset, tables, view, custom role
src/setup_bigquery.py                         entry point: prepare BigQuery; --dry-run, --reconcile-latest
src/deploy_bq_function.py                     entry point: secret + deploy; --show-url, --force
src/show_latest.py                            entry point: newest reading, --source bigquery|bucket
src/config_env.py                             entry point: shell exports, now including the BigQuery names
test/test_bq_core.py                          offline: reading identity, local time, generated SQL
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

A batch that contains the same reading twice is collapsed in Python before the SQL runs, because
MERGE refuses a source that matches one target row more than once.

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

### 5.4 The two-row table, and what it costs

`entries_latest` holds only the newest readings, and the interesting question was how much it
costs to keep it that way on every upload. The obvious implementation — rebuild it from the
history with `ORDER BY ... LIMIT 2` — is the wrong shape: `ORDER BY`/`LIMIT` prunes no
partitions and the dedupe window function forces a pass over all of them, so every upload would
scan the entire history. At 288 readings a day and roughly 440 bytes a row that is about 400 GB a
month in year one, growing linearly and crossing the 1 TiB monthly free allowance in year three.

So the function never reads the history at all. It already holds the reading it just wrote, and
the only other thing "the two most recent" depends on is the two rows already there, so one
`MERGE` against that table alone does the whole job: combine its current contents with the new
rows, rank them, keep the top two, and `WHEN NOT MATCHED BY SOURCE THEN DELETE` the rest.

Measured, not estimated. Each such statement bills **10,485,760 bytes — exactly the 10 MiB
minimum BigQuery charges per table referenced — while processing 38 bytes.** That is about 86 GB
a month, 8% of the free allowance, and it stays there however many years of readings accumulate.

Two behaviours were verified directly before the code was written around them:

* Replaying a reading leaves the table byte-identical.
* A week-late reading does not displace newer rows; it loses the ranking, as it should.

DML is also excluded from the 1,500-table-modifications-per-day limit that a `CREATE OR REPLACE`
would count against, so a backlog flush after the phone has been offline for days cannot exhaust
it. The rate limit that does apply is 25 statements per 10 seconds per table, against one upload
every five minutes.

Failure to update this table never fails the request. It is derived state: the reading is already
safely in the raw table, and the next upload reconciles it. `setup_bigquery.py --reconcile-latest`
rebuilds it from the full history if it ever needs repairing — that one *does* scan the raw table,
which is the cost the MERGE exists to avoid paying routinely.

### 5.5 Permanence, enforced rather than configured

No dataset default expiration, no partition expiration, and nothing in this repo issues a drop.
The part that needed real thought was IAM: `roles/bigquery.dataEditor`, the obvious grant, can
delete tables, which is exactly the authority a public endpoint should not have. So the function
runs as `xdrip2gcp-bq-runtime` holding a custom role, `xdrip2gcpBigQueryWriter`, with five
permissions and no delete among them:

```
bigquery.datasets.get  bigquery.jobs.create  bigquery.tables.get
bigquery.tables.getData  bigquery.tables.updateData
```

The binding is project-level because `bigquery.jobs.create` is a project permission — a job is
not owned by the dataset it reads. The dataset's location is fixed at creation and cannot be
changed later, so it follows `[bucket].location` and the setup script prints it.

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

`bq_core.SCHEMA` is the single definition. The table DDL, the protobuf descriptor and the MERGE
are all generated from it, so they cannot drift, and `setup_bigquery.py` applies a column added
there to the existing tables with `ALTER TABLE ADD COLUMN IF NOT EXISTS` rather than needing a
migration.

Values from the phone are bound as query parameters, never formatted into SQL. The `JSON` column
is the one exception to the mechanism: a JSON column cannot take a bound parameter, so the
document is bound as text and wrapped in `PARSE_JSON(...)` in the statement.

### 5.7 Point xDrip at it

The same arrangement as 4.1 and 4.2, for the other endpoint. First a shell that knows the
instance-specific names:

```bash
conda activate xdrip2gcp
eval "$(python src/config_env.py)"
```

Alongside the Stage 3 variables that adds `XDRIP2GCP_BQ_URL`, `XDRIP2GCP_BQ_FUNCTION`,
`XDRIP2GCP_DATASET`, `XDRIP2GCP_ENTRIES`, `XDRIP2GCP_CURRENT` and `XDRIP2GCP_LATEST`, so nothing
below has to name a generated hostname.

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

To verify, `show_latest.py` now reads either destination and defaults to BigQuery:

```bash
python src/show_latest.py                      # newest reading, from entries_latest
python src/show_latest.py --count 10           # the last ten, from the entries_current view
python src/show_latest.py --raw                # the document as xDrip sent it
python src/show_latest.py --source bucket      # the Stage 3 path, unchanged
```

`show_requests.py` still answers the "is the phone reaching the endpoint at all" question, but
against the Stage 3 function; for this one, read the logs directly:

```bash
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=$XDRIP2GCP_BQ_FUNCTION" \
  --freshness=10m --limit=20 --format="value(textPayload)"
```

And in the console, `BigQuery` → the dataset named by `$XDRIP2GCP_DATASET`.

### 5.8 Verified results

Provisioning and deployment are both idempotent; a second run of each reports only no-ops.

The endpoint, probed live: `/status` and `/experiments/test` answer, a wrong secret gets 401, an
entry with no timestamp gets 400 naming the fields it looked for, an unknown path gets 404 listing
what is supported, and `devicestatus` and `treatments` get 200 with
`x-xdrip2gcp-stored: ignored`. A stored reading returns headers reporting the table, the row
count, and whether the latest-readings table was updated.

A test reading posted twice, then read back:

```
$ python src/show_latest.py --count 5
TIME (LOCAL)         ZONE  MG/DL  DELTA   DIRECTION   DEVICE
2026-09-04 23:11:31  MDT   123    +1.5    Flat        xdrip2gcp-test
2026-09-04 23:08:39  MDT   123    +1.5    Flat        xdrip2gcp-test

newest reading is 9 minutes old (from xdrip2gcp.cgm.entries_current)
```

Four raw rows, two distinct `reading_id`s, two rows from the view, two rows in `entries_latest`:
the at-least-once append and the collapse both doing their jobs. The reading at 23:11 local on
4 September is stored with `reading_date_utc = 2026-09-05` and `reading_date_local = 2026-09-04`,
which is exactly why both dates are columns.

171 tests pass: 132 offline, 39 live.

Two things to know:

* The `python314` runtime was the main risk here, since `google-cloud-bigquery-storage` pulls in
  `protobuf` and `grpcio`. Cloud Build resolved them without trouble; no runtime downgrade was
  needed.
* The verification readings above are in the permanent table, marked `device = 'xdrip2gcp-test'`.
  Rows can be excluded with `WHERE device != 'xdrip2gcp-test'`, or deleted outright — though not
  for the first while after they are written, since rows recently added through the Storage Write
  API resist DML until the streaming buffer flushes.

### 5.9 Run a BigQuery query

Use the BigQuery console in your GCP project, find your way to the `xdrip2gcp` resource, and under `cgm` you should see the created tables. You can now run a query such as

```
SELECT * FROM `xdrip2gcp.cgm.entries` order by reading_date_utc desc LIMIT 1000
```
