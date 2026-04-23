#!/usr/bin/env bash
# One-shot deploy helper for the opendataloader-pdf hybrid server on Cloud Run.
#
# Usage:
#   ./deploy/cloudrun/deploy.sh
#
# Required env vars:
#   PROJECT_ID            - GCP project id
#
# Optional (defaults shown):
#   REGION=us-central1
#   REPOSITORY=opendataloader
#   SERVICE=opendataloader-hybrid
#   GEMINI_MODEL=gemini-3.1-flash
#   GEMINI_SECRET=gemini-api-key
#   USE_VERTEXAI=false    - set to "true" to use Vertex AI instead of the Gemini Developer API

set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
REGION="${REGION:-us-central1}"
REPOSITORY="${REPOSITORY:-opendataloader}"
SERVICE="${SERVICE:-opendataloader-hybrid}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.1-flash}"
GEMINI_SECRET="${GEMINI_SECRET:-gemini-api-key}"
USE_VERTEXAI="${USE_VERTEXAI:-false}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${SERVICE}"

cd "$(git rev-parse --show-toplevel)"

echo ">>> Ensuring Artifact Registry repository exists"
gcloud artifacts repositories describe "${REPOSITORY}" \
  --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPOSITORY}" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --description="opendataloader-pdf container images"

echo ">>> Building image via Cloud Build: ${IMAGE}:latest"
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --tag="${IMAGE}:latest" \
  --file=deploy/cloudrun/Dockerfile \
  .

deploy_args=(
  run deploy "${SERVICE}"
  --project="${PROJECT_ID}"
  --image="${IMAGE}:latest"
  --region="${REGION}"
  --platform=managed
  --cpu=4
  --memory=8Gi
  --concurrency=4
  --timeout=900
  --max-instances=10
  --min-instances=0
  --port=8080
  --allow-unauthenticated
)

if [[ "${USE_VERTEXAI}" == "true" ]]; then
  echo ">>> Deploying with Vertex AI authentication"
  deploy_args+=(
    --set-env-vars="OPENDATALOADER_USE_GEMINI=true,GEMINI_MODEL=${GEMINI_MODEL},GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},EXTRA_ARGS=--use-gemini --gemini-vertexai"
  )
else
  echo ">>> Deploying with Gemini Developer API key from Secret Manager (${GEMINI_SECRET})"
  deploy_args+=(
    --set-env-vars="OPENDATALOADER_USE_GEMINI=true,GEMINI_MODEL=${GEMINI_MODEL},EXTRA_ARGS=--use-gemini"
    --set-secrets="GEMINI_API_KEY=${GEMINI_SECRET}:latest"
  )
fi

gcloud "${deploy_args[@]}"

URL="$(gcloud run services describe "${SERVICE}" \
  --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"

echo ">>> Deployed: ${URL}"
echo ">>> Smoke test: curl ${URL}/health"
