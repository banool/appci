#!/bin/bash
#
# Build a Flutter app's release .aab and upload it to the Play internal track.
#
# This is the canonical implementation, shared by all the apps; each app's
# android/upload.sh is a thin wrapper that sets the env below and execs this.
# Run the wrapper, not this file. It is the local counterpart of the
# app-release-android.yaml CI workflow (which uses the same play_upload.py).
#
# This ONLY uploads the build (it lands on the Play internal track). To send an
# already-uploaded build to the beta testers or the public, use ./promote.sh
# (see its --stage flag).
#
# Signing: `flutter build appbundle` reads android/key.properties (gitignored),
# whose storeFile points at the upload keystore in ~/creds.
#
# Required env (the wrapper sets these):
#   UPLOAD_APP_DIR       absolute path to the Flutter app directory
#   PLAY_PACKAGE_NAME    Android package, e.g. com.banool.auslan_dictionary
# Optional env:
#   PLAY_SERVICE_ACCOUNT_JSON_PATH  Play key (default: <app>/android/play_service_account.json;
#                                   the wrappers point this at ~/creds/play_<app>.json)
#   PLAY_RELEASE_NOTES              notes for the internal release (default below)
#   PLAY_DRY_RUN=1                  build, then plan the upload without doing it
#   PLAY_SKIP_PREFLIGHT=1           skip the version-safety preflight

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for var in UPLOAD_APP_DIR PLAY_PACKAGE_NAME; do
  if [[ -z "${!var:-}" ]]; then
    echo "error: $var must be set (run this via the app's android/upload.sh wrapper)" >&2
    exit 1
  fi
done

# This script uploads only; it takes no arguments.
for arg in "$@"; do
  case "$arg" in
    *) echo "Unknown argument: $arg (upload.sh takes none; use promote.sh to release a build)" >&2; exit 1 ;;
  esac
done

cd "$UPLOAD_APP_DIR"

PLAY_KEY="${PLAY_SERVICE_ACCOUNT_JSON_PATH:-$UPLOAD_APP_DIR/android/play_service_account.json}"
if [[ ! -f "$PLAY_KEY" ]]; then
  echo "error: Play service-account key not found at $PLAY_KEY" >&2
  echo "       Set PLAY_SERVICE_ACCOUNT_JSON_PATH (the wrappers default it to ~/creds/play_<app>.json)." >&2
  exit 1
fi

if [[ ! -f android/key.properties ]]; then
  echo "error: android/key.properties not found — release signing needs it" >&2
  echo "       (gitignored; storeFile should point at the upload keystore in ~/creds)" >&2
  exit 1
fi

# Fail before the build if the store would reject this version anyway.
if [[ "${PLAY_SKIP_PREFLIGHT:-0}" != "1" ]]; then
  echo "==> Checking the version against Google Play..."
  PLAY_SERVICE_ACCOUNT_JSON_PATH="$PLAY_KEY" \
    PLAY_PACKAGE_NAME="$PLAY_PACKAGE_NAME" \
    python3 "$SCRIPT_DIR/preflight.py" --android-only
fi

echo "==> Cleaning build artifacts..."
flutter clean
flutter pub get

echo "==> Building release app bundle..."
flutter build appbundle

echo "==> Uploading to the Play internal track..."
PLAY_SERVICE_ACCOUNT_JSON_PATH="$PLAY_KEY" \
  PLAY_PACKAGE_NAME="$PLAY_PACKAGE_NAME" \
  PLAY_RELEASE_NOTES="${PLAY_RELEASE_NOTES:-Internal build.}" \
  PLAY_SKIP_PREFLIGHT=1 \
  python3 "$SCRIPT_DIR/play_upload.py"

echo "==> Done! Build uploaded to the Play internal track."
echo "    To release it to beta testers or the public, run ./promote.sh."
