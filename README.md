# xdrip2gcp

Continuous glucose monitor readings from an Android phone into Google Cloud, where they can be
queried as history and read as a live value.

[xDrip](https://navid200.github.io/xDrip/) already knows how to upload to
[Nightscout](https://nightscout.github.io/), so this repo puts something on the other end of that
upload: a Cloud Function that speaks enough of the Nightscout REST API for xDrip to be satisfied,
and writes each reading to two places at once.

```
   Dexcom sensor
        |
   xDrip on Android  ──── Nightscout REST, every 5 minutes ────┐
                                                              v
                                            Cloud Function (Python, 2nd gen)
                                                     |               |
                                    append-only      |               |   one document,
                                    history          v               v   overwritten
                                              BigQuery            Firestore
                                            cgm.entries        current/entries
                                                  |                    |
                                        Looker Studio          smart displays,
                                          dashboards          web pages, widgets
```

Two destinations because the two questions are different. **BigQuery** holds every reading forever,
partitioned by day in both UTC and local time, which is what makes in-depth
[Looker Studio](https://cloud.google.com/looker-studio) dashboards cheap to build — overnight
patterns, time in range, day-over-day comparisons. **Firestore** holds a single document with the
newest reading, because asking a data warehouse "what is my blood sugar right now" costs a 10 MiB
minimum per query no matter how little you want back. One document read answers it instead, which
suits a smart home display, a web page, or a phone widget.

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
