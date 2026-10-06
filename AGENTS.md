# Working in this repo

Read **[`agent/architecture.md`](agent/architecture.md)** before making changes. It covers the
design, the conventions that are load-bearing, where secrets live, and the gotchas that have already
cost time once. [`agent/plan.md`](agent/plan.md) is the full build record, stage by stage, and is
where to look for the reasoning behind any particular decision.

The four rules most easily broken by accident:

* **Talk to GCP through the `gcloud` and `bq` CLIs, never a client library.** Local credentials are
  user credentials, not Application Default Credentials, so client libraries will not authenticate.
  Cloud libraries belong only in a deployed function's `requirements.txt`.
* **Keep local code standard library only.** No new dependencies in the conda environment.
* **Everything that touches GCP must be idempotent** and return an `ActionResult`. A second run of
  any script has to report nothing but no-ops.
* **Never commit anything instance-specific**: not the generated bucket suffix, not a function
  hostname, not a spreadsheet ID, and above all not the Nightscout password, all of which live only
  in the git-ignored `resources/config.local.toml`.

Verify a change with the tests and by converging the setup scripts:

```bash
python -m unittest discover -s test -t . -v
python src/setup_bigquery.py && python src/setup_firestore.py && python src/deploy_bq_function.py
python src/show_latest.py
```
