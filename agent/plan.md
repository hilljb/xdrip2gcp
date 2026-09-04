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

The bucket is `xdrip2gcp-test-<suffix>`, e.g. `xdrip2gcp-test-c4278d`. Three reasons it is not
plain `xdrip2gcp_test`:

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

* Bucket `gs://xdrip2gcp-test-c4278d` exists in `us-central1`, `STANDARD` class, uniform access
  on, public access prevention enforced, lifecycle deleting objects after 3 days.
* A second `create_bucket.py` run reports both steps as no-ops.
* `test-data/hello.txt` (92 bytes) and `test-data/sample.json` (500 bytes) write, verify, and
  re-write as no-ops.
* 36 tests pass: 21 offline, 15 live.