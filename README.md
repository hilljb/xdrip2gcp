# xdrip2gcp

Continuous glucose monitor readings from an Android phone into Google Cloud, where they can be
queried as history and read as a live value.

[xDrip](https://navid200.github.io/xDrip/) already knows how to upload to
[Nightscout](https://nightscout.github.io/), so this repo puts something on the other end of that
upload: a Cloud Function that speaks enough of the Nightscout REST API for xDrip to be satisfied,
and writes each reading to three places at once.

```
   Dexcom sensor
        |
   xDrip on Android  ──── Nightscout REST, every 5 minutes ────┐
                                                              v
                                            Cloud Function (Python, 2nd gen)
                              |                      |                      |
         append-only          |      one document,   |      rolling         |
         history              v      overwritten     v      24 hours        v
                        BigQuery                Firestore              Google Sheets
                       cgm.entries           current/entries            recent tab
                            |                        |                       |
                   Looker Studio             smart displays,          Looker Studio
                   deep analysis            pages, widgets          recent-day charts
```

Three destinations because the three questions are different.

**BigQuery** holds every reading forever, partitioned by day in both UTC and local time, which is
what makes in-depth [Looker Studio](https://cloud.google.com/looker-studio) analysis possible —
overnight patterns, time in range, day-over-day comparisons.

**Firestore** holds a single document with the newest reading, because asking a data warehouse "what
is my blood sugar right now" costs a 10 MiB minimum per query no matter how little you want back.
One document read answers it instead, which suits a smart home display, a web page, or a phone
widget.

**Google Sheets** holds a rolling mirror of the last 24 hours. Looker Studio issues a separate
BigQuery query for every chart on a dashboard, which adds up through a day of refreshes; its Sheets
connector does not. So the charts that get looked at most — the recent day, the last hour — read a
few hundred rows from a spreadsheet instead. Unlike the other two, this one is not a resource in the
cloud project: it is an ordinary file in your own Google Drive that the project is given permission
to write to.

## What it costs

Nothing. This is a personal project on a personal GCP project, and every service it touches sits
inside its permanent free allowance:

| Service | Usage | Free allowance |
| --- | --- | --- |
| BigQuery storage | 1.1 MB, growing ~37 MB/year | 10 GiB |
| BigQuery ingestion | a few MB/month | 2 TiB/month |
| Firestore writes | ~288/day | 20,000/day |
| Firestore reads | 1 per check | 50,000/day |
| Cloud Run invocations | ~8,600/month | 2,000,000/month |
| Secret Manager | 1 secret version | 6 versions |
| Sheets API writes | ~576/day | 300/minute |

A billing account still has to be linked, because some of these APIs refuse to run without one.
The only meter that grows with activity rather than with data is Artifact Registry, which keeps the
container image built by each function deploy: currently 67 MB against 0.5 GB free, and even
several gigabytes of old images would cost well under a dollar a month.

## Where to go next

* **[getting-started.md](getting-started.md)** — set this up yourself, from an empty GCP project to
  readings arriving, including how to read the data back out.
* **[agent/plan.md](agent/plan.md)** — the full build record, stage by stage, with the reasoning and
  the measurements behind each decision.
* **[agent/architecture.md](agent/architecture.md)** — the technical design, written for whoever
  (or whatever) picks the project up next.

Everything talks to GCP through the `gcloud` and `bq` command line tools, so the only local
requirement is Python 3.10 or newer and the Cloud SDK. Nothing outside the deployed function needs
a Python dependency beyond the standard library.
