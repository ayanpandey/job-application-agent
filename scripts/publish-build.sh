#!/usr/bin/env bash
#
# Publishes a built app to the download bucket, where an admin console (not part
# of this repository) signs a link to it for each customer's key email.
#
# Builds go to the same bucket as the licences, under the product's own prefix:
#
#     gs://your-bucket-name/resumes/job-agent/builds/<platform>/<version>/<file>
#
# and the console's `builds.py` reads exactly that layout. The two must agree;
# nothing validates it for you, and a build filed anywhere else is invisible.
#
# The upload deliberately does NOT happen in CI. It needs a key for the storage
# project, and that key also grants Vertex AI -- putting it in GitHub Actions
# secrets would mean the model-billing credential lives in a third place, on a
# system neither account administers. So the CI job produces an artifact, the
# operator downloads it, and this script puts it where it belongs.
#
#   macOS:    cd job-agent && JOB_AGENT_API_URL=https://… python build.py
#             ../scripts/publish-build.sh macos 1.0.0 dist/JobAgent.app
#   Windows:  download the workflow artifact, then
#             ./scripts/publish-build.sh windows 1.0.0 ~/Downloads/JobAgent-windows.zip
#
set -euo pipefail

BUCKET="${STORAGE_BUCKET:-your-bucket-name}"
PREFIX="${STORAGE_PREFIX:-resumes/job-agent}"
# The bucket lives in the storage/LLM project, so the upload authenticates as
# that account -- never as the account that merely hosts the Cloud Run service.
ACCOUNT="${GCLOUD_ACCOUNT:-you@example.com}"

usage() {
  echo "usage: $0 <macos|windows> <version> <path-to-build>" >&2
  echo >&2
  echo "  <path-to-build> may be a .zip/.dmg, or a .app bundle which is zipped first." >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
PLATFORM="$1"; VERSION="$2"; SOURCE="$3"

case "${PLATFORM}" in
  macos|windows) ;;
  *) echo "Unknown platform '${PLATFORM}'. Use macos or windows." >&2; exit 2 ;;
esac

[[ -e "${SOURCE}" ]] || { echo "No such build: ${SOURCE}" >&2; exit 1; }

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# A .app is a directory; customers need one file, and zipping preserves the
# bundle's structure and its executable bits where a tarball in Safari does not.
CLEANUP=""
if [[ -d "${SOURCE}" ]]; then
  say "Zipping the .app bundle"
  STAGE="$(mktemp -d)"
  CLEANUP="${STAGE}"
  ARCHIVE="${STAGE}/JobAgent-macos-${VERSION}.zip"
  ditto -c -k --sequesterRsrc --keepParent "${SOURCE}" "${ARCHIVE}"
  SOURCE="${ARCHIVE}"
fi

FILENAME="$(basename "${SOURCE}")"
DEST="gs://${BUCKET}/${PREFIX}/builds/${PLATFORM}/${VERSION}/${FILENAME}"

say "Checking the upload account can actually authenticate"
# A user account's gcloud token expires and can only be refreshed
# interactively, which a script cannot do -- so this fails at the `cp`, after
# the .app has been zipped, with a reauthentication error that reads like a
# permissions problem. The service-account key is also an admin on this bucket
# and belongs to the same storage project, so it is a legitimate uploader
# rather than a workaround; it just has to be named.
if ! gcloud storage ls "gs://${BUCKET}" --account="${ACCOUNT}" >/dev/null 2>&1; then
  echo "  ${ACCOUNT} cannot authenticate (its token has most likely expired)." >&2
  echo >&2
  echo "  Either refresh it:" >&2
  echo "      gcloud auth login ${ACCOUNT}" >&2
  echo >&2
  echo "  ...or publish as the storage project's service account, which is an" >&2
  echo "  admin on this bucket:" >&2
  echo "      GCLOUD_ACCOUNT=PROJECT_NUMBER-compute@developer.gserviceaccount.com \\" >&2
  echo "        $0 ${PLATFORM} ${VERSION} <path-to-build>" >&2
  exit 1
fi
echo "    ${ACCOUNT} OK"

say "Checking the build carries no credentials"
# Cheap but worth it: build.py already refuses to bundle these, and this is the
# last point before the artefact becomes downloadable by a customer.
if unzip -l "${SOURCE}" 2>/dev/null | grep -Eq 'service_account\.json|prompts\.yaml|profile\.yaml|\.env$'; then
  echo "  REFUSING TO PUBLISH: the archive contains a credential or the prompt templates." >&2
  exit 1
fi
echo "    clean"

say "Uploading to ${DEST}"
gcloud storage cp "${SOURCE}" "${DEST}" --account="${ACCOUNT}"

[[ -n "${CLEANUP}" ]] && rm -rf "${CLEANUP}"

say "Published"
echo "  ${DEST}"
echo
echo "The admin console now offers this as the latest ${PLATFORM} build, and"
echo "every key minted from here on carries a signed link to it."
