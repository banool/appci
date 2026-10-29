#!/usr/bin/env bash
# Drive the multi-device e2e suite: two devices, four phases, one worker.
#
#   Phase A (device A): owner shares a list, mints an editor invite.
#   Phase B (device B): editor accepts the invite, adds a word.
#   Phase C (device A): owner re-signs-in on a fresh install (the test
#                       runner reinstalls per run), recovers the list,
#                       sees the edit, renames the list.
#   Phase D (device B): editor likewise recovers without the consumed
#                       invite and sees the rename; full convergence.
#
# Then four runs that need the worker but only one device and one person
# each, so they ride along here rather than in the single-device suites:
#
#   Solo revise  (device A): someone shares their default list under another
#                       name and revises from it straight away, finding it
#                       under that name everywhere.
#   Solo upgrade (device B): an install from before Favourites became an
#                       ordinary list, with favourites shared and its owner
#                       signed in, takes the update and goes on syncing.
#   Solo imports (device A): a list its owner shared as "search for" (a name
#                       a list on the device can't have) arrives from their
#                       other device, and search still works.
#   Solo follow  (device B): following a list from its share link lands on
#                       the Subscribed tab with the list marked new, once.
#
# Then account sync (everything a signed-in person has, on every device).
# Each run starts from a fresh install, so what a phase needs from "another
# device" mid-run is done over HTTP by the suite; phase E carries its state
# between the two real apps through the account itself:
#
#   Phase E1 (device A): a list called Animals, then sign in.
#   Phase E2 (device B): its own Animals with other signs, sign in: one
#                       Animals on the account holding both.
#   Phase E3 (device A): a new install with yet another Animals: the one
#                       list, with every sign.
#   Phase F  (device B): both edit the same list offline: the union, once;
#                       a sign removed on one and saved again on the other
#                       stays.
#   Phase G  (device A): a list deleted elsewhere goes; one with unsent
#                       changes here stays as a list of its own; a list
#                       deleted here goes from the account.
#   Phase H1 (device A): answers from earlier sessions and a session now
#                       go up; a random-mode session is counted.
#   Phase H2 (device B): A's answers count in its due cards and progress
#                       page; its own session goes up; its random-mode
#                       session adds to A's on the progress page.
#   Phase H3 (device A): a new install has every answer from both, and the
#                       random-mode total of both.
#   Solo old stop sharing (device B): a list shared from a build before
#                       account sync and "unshared" by it stays on the
#                       account, private.
#   Solo upgrade sync (device A): an install from before account sync takes
#                       the update and signs in; its lists, history and
#                       progress come through, once.
#   Solo offline/online (device A, then B): with a proxy inside the app
#                       taken down, then turned into a black hole, a list
#                       made, filled, renamed, revised and a setting changed
#                       through the UI all show at once with nothing said;
#                       back online it all goes up (the sync status sheet
#                       says so), and device B, a fresh install, has it.
#   Follow 1 to 3 (A, B, A): A follows a shared list from its link; B has
#                       it in Subscribed and unfollows; A (its install kept)
#                       loses it.
#   Solo account switch (device A): signed in as A, then B, then A again
#                       on one device: A's lists plus the one made under B,
#                       nothing doubled.
#   Deletion 1 to 3 (B, A, B): B signed in with a list and answers; A
#                       deletes the account; B (its install kept) is signed
#                       out by its next sync, keeps everything, and signing
#                       in again makes a new account from it.
#   Solo heavy account (device B): 20 lists of 50 signs and 2,000 answers;
#                       taps answered during the merge, Revision quick after
#                       (timings in the log as MD_OUT HEAVY_*).
#
# This is the canonical script; each app's integration_test/multi_device/run.sh
# is a thin wrapper that sets the per-app env below and execs this.
#
# Required env (the wrapper sets these):
#   MD_APP_DIR      absolute path to the Flutter app directory
#   MD_BUNDLE_ID    iOS bundle id, e.g. com.banool.auslanDictionary
#   MD_ANDROID_PKG  Android package, e.g. com.banool.auslan_dictionary
#   MD_APP_ID       worker app id (x-app-id header), e.g. auslan / slsl
#
# Optional env:
#   FLUTTER             flutter binary (defaults to PATH, then ~/.development)
#   MD_API_BASE_URL     worker base URL (default http://localhost:8787)
#   MD_TEST_AUTH_TOKEN  must match the worker env's TEST_AUTH_TOKEN
#
# Prereqs:
#   - A local worker: cd <dictionary_backend>/workers && bunx wrangler dev --env dev
#   - Two booted devices (iOS simulators and/or Android emulators).
#
# Usage (via the app wrapper):
#   run.sh                       # picks the first two booted devices
#   run.sh <deviceA> <deviceB>   # explicit flutter device ids
#   run.sh --fresh ...           # uninstall the app from both first
#   MD_SKIP=solo-follow run.sh   # leave out runs by name (comma-separated)
#
# Android emulators are handled automatically: they reach the host's
# worker via 10.0.2.2 instead of localhost.

set -euo pipefail

for var in MD_APP_DIR MD_BUNDLE_ID MD_ANDROID_PKG MD_APP_ID; do
  if [[ -z "${!var:-}" ]]; then
    echo "error: $var must be set (run this via the app's integration_test/multi_device/run.sh wrapper)" >&2
    exit 1
  fi
done

FLUTTER=${FLUTTER:-$(command -v flutter || echo "$HOME/.development/flutter/bin/flutter")}
API_BASE=${MD_API_BASE_URL:-http://localhost:8787}
TEST_AUTH_TOKEN=${MD_TEST_AUTH_TOKEN:-dev-integration-test-token-please-override}
APP_DIR="$MD_APP_DIR"
BUNDLE_ID="$MD_BUNDLE_ID"
ANDROID_PKG="$MD_ANDROID_PKG"

cd "$APP_DIR"

FRESH=0
if [[ "${1:-}" == "--fresh" ]]; then FRESH=1; shift; fi

# --- Worker health -----------------------------------------------------------
if ! curl -fsS -m 5 "$API_BASE/v1/health" >/dev/null 2>&1; then
  echo "error: no worker at $API_BASE. Start one with:" >&2
  echo "  cd <dictionary_backend>/workers && bunx wrangler dev --env dev" >&2
  exit 1
fi

# --- Pick devices ------------------------------------------------------------
if [[ $# -ge 2 ]]; then
  DEVICE_A=$1; DEVICE_B=$2
else
  # Booted iOS simulators first, then running Android emulators.
  # (Portable across the macOS default bash 3.2 — no mapfile there.)
  DEVICES=()
  while IFS= read -r line; do DEVICES+=("$line"); done < <(
    { xcrun simctl list devices booted 2>/dev/null \
        | grep -Eo '[0-9A-F-]{36}' || true;
      adb devices 2>/dev/null | awk '/^emulator-/{print $1}' || true; } | head -2
  )
  if [[ ${#DEVICES[@]} -lt 2 ]]; then
    echo "error: need two booted devices (have ${#DEVICES[@]})." >&2
    echo "Boot two iOS simulators (xcrun simctl boot <udid>) and/or" >&2
    echo "Android emulators, or pass explicit device ids." >&2
    exit 1
  fi
  DEVICE_A=${DEVICES[0]}; DEVICE_B=${DEVICES[1]}
fi

# Android emulators reach the host loopback via 10.0.2.2.
base_url_for() {
  case "$1" in
    emulator-*) echo "${API_BASE/localhost/10.0.2.2}" ;;
    *)          echo "$API_BASE" ;;
  esac
}

RUN_ID=$(date +%s)
LOG_DIR=$(mktemp -d "${TMPDIR:-/tmp}/md-e2e.XXXXXX")
echo "== multi-device e2e =="
echo "   app:      $MD_APP_ID ($APP_DIR)"
echo "   device A: $DEVICE_A"
echo "   device B: $DEVICE_B"
echo "   worker:   $API_BASE"
echo "   run id:   $RUN_ID"
echo "   logs:     $LOG_DIR"

if [[ $FRESH -eq 1 ]]; then
  echo "-- removing existing installs (--fresh)"
  for D in "$DEVICE_A" "$DEVICE_B"; do
    case "$D" in
      emulator-*) adb -s "$D" uninstall "$ANDROID_PKG" >/dev/null 2>&1 || true ;;
      *)          xcrun simctl uninstall "$D" "$BUNDLE_ID" >/dev/null 2>&1 || true ;;
    esac
  done
fi

# --- Server-side cleanup of test users from previous runs --------------------
curl -fsS -m 10 -X POST "$API_BASE/v1/test/wipe" \
  -H "x-app-id: $MD_APP_ID" -H "x-test-auth-token: $TEST_AUTH_TOKEN" \
  >/dev/null || echo "warning: test wipe failed (continuing — run ids are fresh anyway)"

# --- Phase runner -------------------------------------------------------------
# run_phase <name> <device> <test-file> [extra --dart-define args...]
run_phase() {
  local name=$1 device=$2 file=$3; shift 3
  local log="$LOG_DIR/$name.log"
  if [[ ",${MD_SKIP:-}," == *",$name,"* ]]; then
    echo "-- $name skipped (MD_SKIP)"
    return 0
  fi
  echo "-- $name on $device"
  if ! "$FLUTTER" test "integration_test/multi_device/$file" -d "$device" \
      --dart-define=MD_RUN_ID="$RUN_ID" \
      --dart-define=MD_API_BASE_URL="$(base_url_for "$device")" \
      --dart-define=MD_TEST_AUTH_TOKEN="$TEST_AUTH_TOKEN" \
      "$@" 2>&1 | tee "$log"; then
    echo "FAIL: $name (log: $log)" >&2
    exit 1
  fi
}

scrape() { # scrape <log-name> <key>
  sed -n "s/^.*MD_OUT $2=//p" "$LOG_DIR/$1.log" | tail -1
}

run_phase phase-a "$DEVICE_A" phase_a_owner_shares_test.dart
LIST_ID=$(scrape phase-a LIST_ID)
INVITE_URL=$(scrape phase-a INVITE_URL)
[[ -n "$LIST_ID" && -n "$INVITE_URL" ]] || { echo "FAIL: phase A emitted no invite" >&2; exit 1; }

run_phase phase-b "$DEVICE_B" phase_b_editor_joins_test.dart \
  --dart-define=MD_INVITE_URL="$INVITE_URL"
EDITOR_KEY=$(scrape phase-b EDITOR_ADDED_KEY)
[[ -n "$EDITOR_KEY" ]] || { echo "FAIL: phase B emitted no editor key" >&2; exit 1; }

run_phase phase-c "$DEVICE_A" phase_c_owner_converges_test.dart \
  --dart-define=MD_LIST_ID="$LIST_ID" --dart-define=MD_EDITOR_KEY="$EDITOR_KEY"

run_phase phase-d "$DEVICE_B" phase_d_editor_converges_test.dart \
  --dart-define=MD_LIST_ID="$LIST_ID" --dart-define=MD_EDITOR_KEY="$EDITOR_KEY"

run_phase solo-revise "$DEVICE_A" solo_owner_revises_test.dart

run_phase solo-upgrade "$DEVICE_B" upgrade_shared_favourites_test.dart

run_phase solo-imports "$DEVICE_A" solo_owner_imports_test.dart

run_phase solo-follow "$DEVICE_B" solo_follow_link_test.dart

# --- Account sync ---------------------------------------------------------------
run_phase phase-e1 "$DEVICE_A" phase_e1_first_device_test.dart
run_phase phase-e2 "$DEVICE_B" phase_e2_second_device_test.dart
run_phase phase-e3 "$DEVICE_A" phase_e3_first_device_again_test.dart
run_phase phase-f "$DEVICE_B" phase_f_offline_edits_test.dart
run_phase phase-g "$DEVICE_A" phase_g_delete_elsewhere_test.dart
run_phase phase-h1 "$DEVICE_A" phase_h1_first_device_test.dart
run_phase phase-h2 "$DEVICE_B" phase_h2_second_device_test.dart
run_phase phase-h3 "$DEVICE_A" phase_h3_first_device_again_test.dart
run_phase solo-old-stop-sharing "$DEVICE_B" solo_old_build_stop_sharing_test.dart
run_phase solo-upgrade-sync "$DEVICE_A" upgrade_sync_from_old_install_test.dart
run_phase solo-offline-online "$DEVICE_A" solo_offline_online_test.dart
run_phase solo-offline-online-after "$DEVICE_B" solo_offline_online_after_test.dart

# Follows across devices. Follow 1 leaves its install (--no-uninstall) for
# follow 3, so nothing else runs on device A in between.
run_phase follow-1 "$DEVICE_A" follow_first_device_test.dart --no-uninstall
FOLLOW_LIST_ID=$(scrape follow-1 MD_FOLLOW_LIST_ID)
[[ -n "$FOLLOW_LIST_ID" || ",${MD_SKIP:-}," == *",follow-1,"* ]] || {
  echo "FAIL: follow 1 emitted no list id" >&2
  exit 1
}
run_phase follow-2 "$DEVICE_B" follow_second_device_test.dart \
  --dart-define=MD_FOLLOW_LIST_ID="$FOLLOW_LIST_ID"
run_phase follow-3 "$DEVICE_A" follow_first_device_again_test.dart \
  --dart-define=MD_FOLLOW_LIST_ID="$FOLLOW_LIST_ID"

run_phase solo-account-switch "$DEVICE_A" solo_account_switch_test.dart

# Account deletion seen from the other device. Deletion 1 leaves its
# install (--no-uninstall) for deletion 3, so nothing else runs on device B
# in between.
run_phase deletion-1 "$DEVICE_B" deletion_other_device_before_test.dart --no-uninstall
run_phase deletion-2 "$DEVICE_A" deletion_deleting_device_test.dart
run_phase deletion-3 "$DEVICE_B" deletion_other_device_after_test.dart

run_phase solo-heavy-account "$DEVICE_B" solo_heavy_account_test.dart

if [[ -n "${MD_SKIP:-}" ]]; then
  echo "== multi-device e2e: every run not skipped passed (skipped: $MD_SKIP) =="
else
  echo "== multi-device e2e: ALL PHASES AND SOLO RUNS PASSED =="
fi
