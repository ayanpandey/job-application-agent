#!/usr/bin/env bash
#
# Deploys the licence proxy to Cloud Run.
#
# Billing is split on purpose. Cloud Run, Cloud Build and Artifact Registry bill
# to PROJECT_ID, which the operator owns and administers. Vertex AI bills to
# VERTEX_PROJECT_ID, which the operator does NOT administer -- they hold only a
# service-account key for it. That key is kept in Secret Manager and mounted as
# a file at runtime, so it exists on this service and nowhere else: not in the
# image, not in an environment variable, and above all not in a customer's
# download, which is where a copy of it used to end up.
#
# The mounted key is what reaches both Vertex AI and the storage bucket, so
# licences, usage counters, telemetry and stored resumes all land in
# VERTEX_PROJECT_ID. The runtime identity in PROJECT_ID can read one secret and
# do nothing else -- see the role grant and the assertion further down.
#
#   gcloud auth login          # the account that owns PROJECT_ID
#   ./deploy.sh
#
set -euo pipefail

# The project that hosts the container -- and only that.
PROJECT_ID="${PROJECT_ID:-your-host-project}"
VERTEX_PROJECT_ID="${VERTEX_PROJECT_ID:-your-vertex-project}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-job-agent-proxy}"
RUNTIME_SA="${RUNTIME_SA:-job-agent-proxy}"
GCP_LOCATION="${GCP_LOCATION:-global}"
# Every object the service persists goes here. The bucket lives in
# VERTEX_PROJECT_ID and is reached with the mounted key, so no customer data is
# ever written to the project hosting the service.
STORAGE_BUCKET="${STORAGE_BUCKET:-your-bucket-name}"
# Namespaces this product's objects so the bucket can also hold the resume
# corpus and the reels files without anything colliding.
STORAGE_PREFIX="${STORAGE_PREFIX:-resumes/job-agent}"
SECRET_NAME="${SECRET_NAME:-vertex-sa-key}"
# Where Cloud Run mounts the key. Must match VERTEX_SA_KEY_PATH below.
SECRET_MOUNT="/secrets/vertex-sa/key.json"

SA_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
ACCOUNT_FLAG="${GCLOUD_ACCOUNT:+--account=$GCLOUD_ACCOUNT}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Cloud Run (code only)  : ${PROJECT_ID} (${REGION})"
say "Vertex AI + storage   : ${VERTEX_PROJECT_ID} (gs://${STORAGE_BUCKET})"

say "Enabling APIs (safe to re-run)"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project "${PROJECT_ID}" ${ACCOUNT_FLAG}

say "Ensuring runtime service account"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT_ID}" ${ACCOUNT_FLAG} >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SA}" \
    --display-name="Job Agent licence proxy" --project "${PROJECT_ID}" ${ACCOUNT_FLAG}
else
  echo "    already present"
fi

# Least privilege, and it really is minimal: this identity may read one secret
# and nothing else. Notably it has NO aiplatform role and NO storage role --
# both Vertex AI and the bucket are reached with the mounted key instead, so a
# compromise of the runtime identity yields neither model access nor data.
# A freshly created service account is not immediately visible to the IAM
# policy API, which rejects the binding with "does not exist" -- on a new
# project this fails every time, because the account was made seconds earlier.
# Retry rather than make the operator run the script twice.
say "Granting roles"
for ROLE in roles/secretmanager.secretAccessor; do
  for ATTEMPT in 1 2 3 4 5 6; do
    if gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
         --member="serviceAccount:${SA_EMAIL}" --role="${ROLE}" \
         --condition=None --quiet ${ACCOUNT_FLAG} >/dev/null 2>&1; then
      echo "    ${ROLE}"
      break
    fi
    if [[ "${ATTEMPT}" == "6" ]]; then
      echo "  Could not grant ${ROLE} to ${SA_EMAIL} after 6 attempts." >&2
      exit 1
    fi
    echo "    waiting for ${SA_EMAIL} to propagate (attempt ${ATTEMPT})..."
    sleep 10
  done
done

# Belt and braces. The service refuses to use ADC for Vertex AI in code, and
# this makes sure Google would refuse it too: if the runtime identity ever gains
# aiplatform.user, model calls could be billed to PROJECT_ID, which is the one
# outcome this whole split exists to prevent.
say "Asserting the runtime identity CANNOT call Vertex AI"
if gcloud projects get-iam-policy "${PROJECT_ID}" ${ACCOUNT_FLAG} \
     --flatten="bindings[].members" \
     --filter="bindings.members:${SA_EMAIL} AND bindings.role:roles/aiplatform" \
     --format="value(bindings.role)" 2>/dev/null | grep -q aiplatform; then
  echo
  echo "  REFUSING TO DEPLOY: ${SA_EMAIL} holds an aiplatform role."
  echo "  That would let model calls bill to ${PROJECT_ID}. Remove it with:"
  echo
  echo "    gcloud projects remove-iam-policy-binding ${PROJECT_ID} \\"
  echo "      --member=serviceAccount:${SA_EMAIL} --role=roles/aiplatform.user"
  echo
  exit 1
fi
echo "    confirmed: no aiplatform role on ${SA_EMAIL}"

say "Checking the Vertex AI key secret"
if ! gcloud secrets describe "${SECRET_NAME}" --project "${PROJECT_ID}" ${ACCOUNT_FLAG} >/dev/null 2>&1; then
  cat <<EOF

  Secret '${SECRET_NAME}' does not exist yet. Create it from the service-account
  key for ${VERTEX_PROJECT_ID}, then re-run this script:

    gcloud secrets create ${SECRET_NAME} --replication-policy=automatic \\
      --project ${PROJECT_ID}

    gcloud secrets versions add ${SECRET_NAME} \\
      --data-file=/path/to/service_account.json \\
      --project ${PROJECT_ID}

EOF
  exit 1
fi
echo "    present"

say "Deploying ${SERVICE}"
# --allow-unauthenticated is correct: customers authenticate with a licence key
# this service validates itself, not with Google IAM.
gcloud run deploy "${SERVICE}" \
  --source . \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  ${ACCOUNT_FLAG} \
  --service-account "${SA_EMAIL}" \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=10 \
  --memory=512Mi --cpu=1 --timeout=120 --concurrency=20 \
  --set-secrets "${SECRET_MOUNT}=${SECRET_NAME}:latest" \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},VERTEX_PROJECT_ID=${VERTEX_PROJECT_ID},GCP_LOCATION=${GCP_LOCATION},VERTEX_SA_KEY_PATH=${SECRET_MOUNT},FORBIDDEN_VERTEX_PROJECTS=${PROJECT_ID},STORAGE_BUCKET=${STORAGE_BUCKET},STORAGE_PREFIX=${STORAGE_PREFIX}"

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT_ID}" ${ACCOUNT_FLAG} --format='value(status.url)')"

say "Deployed"
echo "  Service URL : ${URL}"
echo "  Health      : curl ${URL}/health"
echo
echo "Next:"
echo "  python admin.py mint --name 'Client Name' --email client@example.com"
echo "  cd ../job-agent && JOB_AGENT_API_URL=${URL} python build.py"
