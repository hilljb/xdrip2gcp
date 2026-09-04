# Plan to Move xDrip Data to GCP

We're going to move data from [xDrip](https://navid200.github.io/xDrip/) into a GCP project. This will follow several steps to get everything working and tested. Our end goal is to get data into [BigQuery](https://cloud.google.com/bigquery) and use [Looker Studio / Data Studio](https://cloud.google.com/data-studio). Our plan is to use the [Nightscout](https://nightscout.github.io/) API capabilities in xDrip to send data to a GCP function, using the data from there.

This plan will be broken into stages, some performed by the developer and some performed by an agent.

## Stage 1: GCP Project Setup and Local Dependencies (Developer) ← In-Progress

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

### 1.3 Auth `gcloud` and make sure it can see the project

Auth:
```bash
gcloud auth login
```

Can `gcloud` see your project:
```bash
gcloud projects list
```

Use the `PROJECT_ID` to set the current default project:
```
gcloud config set project <PROJECT_ID>
```