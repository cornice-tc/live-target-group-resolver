#!/bin/bash
# Build dist/live-target-group-resolver.zip. The zip ships the two root modules verbatim —
# no compiled deps (boto3/urllib3 are in the Lambda runtime), so no cross-compile.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="${REPO_DIR}/dist"
ZIP_PATH="${DIST_DIR}/live-target-group-resolver.zip"

rm -f "${ZIP_PATH}"
mkdir -p "${DIST_DIR}"

# zip embeds each file's mtime, so the staged copies get a fixed stamp and
# -X drops platform-specific attributes — rebuilding the same commit yields
# byte-identical artifacts.
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT
cp "${REPO_DIR}/live_target_group_resolver.py" "${REPO_DIR}/cfnresponse.py" "${STAGE_DIR}/"
touch -t 200001010000 "${STAGE_DIR}/live_target_group_resolver.py" "${STAGE_DIR}/cfnresponse.py"

(
    cd "${STAGE_DIR}" &&
        zip -X -j "${ZIP_PATH}" live_target_group_resolver.py cfnresponse.py >/dev/null
)

echo "Built ${ZIP_PATH}:"
sha256sum "${ZIP_PATH}"
