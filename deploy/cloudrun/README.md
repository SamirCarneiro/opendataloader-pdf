# Cloud Run deployment — hybrid server with Gemini 3.1 Flash

This directory contains everything needed to run the `opendataloader-pdf-hybrid`
FastAPI server on Google Cloud Run, with Gemini 3.1 Flash powering picture
descriptions.

## What's in the box

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds a Python 3.11 image bundling Docling + `google-genai`. Pre-downloads Docling models during build so cold starts stay fast. |
| `.dockerignore` | Keeps the build context small (only `python/opendataloader-pdf/` is shipped). |
| `.gcloudignore` | Keeps `gcloud builds submit` uploads small. |
| `cloudbuild.yaml` | Cloud Build pipeline: build → push → `gcloud run deploy`. |
| `service.yaml` | Knative service manifest for `gcloud run services replace`. |
| `deploy.sh` | One-shot helper: creates Artifact Registry, builds, and deploys. |

## Architecture

```
Java CLI                            Cloud Run (this service)
┌─────────────────┐    HTTPS       ┌─────────────────────────────┐
│ Triage selects  │  multipart     │  FastAPI hybrid_server      │
│ complex pages   ├───────────────►│    │                        │
│                 │                │    ▼                        │
│ DoclingDocument │                │  Docling (tables, OCR)      │
│ JSON back       │◄───────────────┤    │                        │
└─────────────────┘                │    ▼                        │
                                   │  Gemini 3.1 Flash           │
                                   │  (picture descriptions)     │
                                   └─────────────────────────────┘
```

The Java client already knows how to talk to `/v1/convert/file` (see
`DoclingFastServerClient`). Point it at the Cloud Run URL with
`--hybrid-url https://…run.app`.

## One-time setup

```bash
export PROJECT_ID=your-gcp-project
export REGION=us-central1

# Enable APIs.
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    secretmanager.googleapis.com \
    aiplatform.googleapis.com \
    --project="${PROJECT_ID}"

# Create the Gemini API key secret (Developer API path).
printf '%s' "YOUR_GEMINI_API_KEY" | \
  gcloud secrets create gemini-api-key \
    --project="${PROJECT_ID}" \
    --replication-policy=automatic \
    --data-file=-

# Grant the Cloud Run runtime SA access to the secret.
RUN_SA="$(gcloud iam service-accounts list \
    --project="${PROJECT_ID}" \
    --filter='email~compute@developer.gserviceaccount.com' \
    --format='value(email)')"

gcloud secrets add-iam-policy-binding gemini-api-key \
    --project="${PROJECT_ID}" \
    --member="serviceAccount:${RUN_SA}" \
    --role=roles/secretmanager.secretAccessor
```

## Deploy

### Option A — one-shot script

```bash
PROJECT_ID=your-gcp-project ./deploy/cloudrun/deploy.sh
```

### Option B — Cloud Build

```bash
gcloud builds submit \
    --project="${PROJECT_ID}" \
    --config=deploy/cloudrun/cloudbuild.yaml \
    --substitutions=_REGION=${REGION},_REPOSITORY=opendataloader,_SERVICE=opendataloader-hybrid
```

### Option C — Knative manifest

```bash
# Fill in placeholders in service.yaml first.
gcloud run services replace deploy/cloudrun/service.yaml \
    --project="${PROJECT_ID}" --region="${REGION}"
```

## Smoke test

```bash
URL="$(gcloud run services describe opendataloader-hybrid \
    --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"

curl -fsS "${URL}/health"

curl -fsS -X POST "${URL}/v1/convert/file" \
    -F "files=@samples/example.pdf" \
    -F "page_ranges=1-2" | jq '.status, .processing_time'
```

## Using Vertex AI instead of the Developer API

When you'd rather authenticate via Workload Identity and skip the API-key
secret, redeploy with:

```bash
USE_VERTEXAI=true PROJECT_ID=your-gcp-project ./deploy/cloudrun/deploy.sh
```

The runtime service account needs `roles/aiplatform.user` on the project.

## Runtime configuration

These env vars are read by `hybrid_server.py` at startup:

| Variable | Effect |
|----------|--------|
| `PORT` | Listen port (Cloud Run sets this to `8080`). |
| `OPENDATALOADER_USE_GEMINI` | `true` → enable Gemini picture descriptions. |
| `GEMINI_API_KEY` | Gemini Developer API key. |
| `GEMINI_MODEL` | Override the default `gemini-3.1-flash`. |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` → route through Vertex AI. |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | Vertex AI targeting. |
| `EXTRA_ARGS` | Appended verbatim to the `opendataloader-pdf-hybrid` command line (e.g. `--force-ocr --ocr-lang en`). |

## Wiring the Java CLI

Once deployed, point the local CLI at the service:

```bash
opendataloader-pdf \
    --hybrid docling-fast \
    --hybrid-url "${URL}" \
    --hybrid-mode full \
    --enrich-picture-description \
    input.pdf
```

**Important:** `--enrich-picture-description` is only honoured by the backend.
Per `CLAUDE.md`, the client must also pass `--hybrid-mode full` for enrichments
to take effect.

## Cost & performance notes

- `cpu=4, memory=8Gi, concurrency=4` is a reasonable starting point; Docling
  tables dominate CPU, Gemini calls dominate wall-clock.
- Cold start is ~15-25s once models are cached in the image; bump
  `min-instances=1` if that matters for your traffic.
- Each picture costs one Gemini call. Disable `--enrich-picture-description`
  on the client side when you don't need captions.
