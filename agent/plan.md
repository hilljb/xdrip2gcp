# Plan to Move xDrip Data to GCP

We're going to move data from [xDrip](https://navid200.github.io/xDrip/) into a GCP project. This will follow several steps to get everything working and tested. Our end goal is to get data into [BigQuery](https://cloud.google.com/bigquery) and use [Looker Studio / Data Studio](https://cloud.google.com/data-studio). Our plan is to use the [Nightscout](https://nightscout.github.io/) API capabilities in xDrip to send data to a GCP function, using the data from there.

This plan will be broken into stages, some performed by the developer and some performed by an agent. This will be:

1. Set up local dependencies (mainly for testing) and a GCP project.
2. Write to a GCP storage bucket using the `gcloud` cli.
3. Make a GCP function that can write to the bucket and use the `gcloud` cli to access it.
4. Send data from xDrip through that GCP function and into the bucket from a phone.

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
src/show_latest.py                            entry point: the newest reading in the bucket
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