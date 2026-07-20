#!/bin/bash
#
# Mirror local release credentials into each app repo's GitHub Actions secrets.
#
# The local files are the single source of truth: key material lives in ~/creds
# (see its README), and each app's gitignored ios/secrets.env and
# android/key.properties point at it. Rotating a credential = update ~/creds,
# re-run this. Hardcoded to the three apps on purpose; this is not a general
# tool.
#
# Usage:
#   ./sync_gha_secrets.sh auslan|slsl|kombio   # one app
#   ./sync_gha_secrets.sh --all                # all three
#
# Secrets set per repo (the shapes the appci workflows expect):
#   APP_STORE_CONNECT_API_KEY_P8      raw PEM of the ASC API key (.p8 from ios/secrets.env)
#   APP_STORE_CONNECT_API_KEY_ID      derived from the AuthKey_<ID>.p8 filename
#   APP_STORE_CONNECT_API_ISSUER_ID   from ios/secrets.env
#   UPLOAD_KEYSTORE                   base64 of the upload keystore in ~/creds
#   KEY_PROPERTIES                    base64 of android/key.properties with storeFile
#                                     rewritten to ../upload_keystore.jks (where the
#                                     android workflow decodes the keystore to)
#   ANDROID_SERVICE_ACCOUNT_JSON      contents of the Play service-account JSON
#
# Afterwards it lists any repo secrets outside the known-good set so stale ones
# get noticed instead of accumulating.

set -euo pipefail

GITHUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

sync_app() {
  local app="$1" repo app_dir play_json keystore extra_ok
  case "$app" in
    auslan)
      repo="banool/auslan_dictionary"
      app_dir="$GITHUB_DIR/auslan_dictionary"
      play_json="$HOME/creds/play_auslan.json"
      keystore="$HOME/creds/auslan_upload_keystore.jks"
      extra_ok="CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID R2_ACCESS_KEY_ID R2_ACCOUNT_ID R2_SECRET_ACCESS_KEY"
      ;;
    slsl)
      repo="banool/slsl_dictionary"
      app_dir="$GITHUB_DIR/slsl_dictionary/frontend"
      play_json="$HOME/creds/play_slsl.json"
      keystore="$HOME/creds/slsl_upload_keystore.jks"
      extra_ok="CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID DOCKER_HUB_USERNAME DOCKER_HUB_PERSONAL_ACCESS_TOKEN"
      ;;
    kombio)
      repo="banool/kombio_scorekeeper"
      app_dir="$GITHUB_DIR/kombio_scorekeeper"
      play_json="$HOME/creds/play_kombio.json"
      keystore="$HOME/creds/kombio_upload_keystore.jks"
      extra_ok=""
      ;;
    *) echo "unknown app: $app (want auslan, slsl, or kombio)" >&2; return 1 ;;
  esac

  echo "=== $app -> $repo"

  # --- App Store Connect: issuer + key path come from the app's secrets.env.
  local secrets_env="$app_dir/ios/secrets.env"
  if [[ -f "$secrets_env" ]]; then
    local issuer key_path key_id kf
    issuer="$(bash -c ". '$secrets_env' >/dev/null 2>&1; printf '%s' \"\${APP_STORE_CONNECT_API_ISSUER_ID:-}\"")"
    key_path="$(bash -c ". '$secrets_env' >/dev/null 2>&1; printf '%s' \"\${API_KEY_PATH:-}\"")"
    if [[ -n "$issuer" && -f "$key_path" ]]; then
      kf="$(basename "$key_path")"
      key_id="${kf#AuthKey_}"; key_id="${key_id%.p8}"
      gh secret set APP_STORE_CONNECT_API_KEY_P8 -R "$repo" < "$key_path"
      gh secret set APP_STORE_CONNECT_API_KEY_ID -R "$repo" --body "$key_id"
      gh secret set APP_STORE_CONNECT_API_ISSUER_ID -R "$repo" --body "$issuer"
      echo "    ASC trio set (key $key_id)"
    else
      echo "    WARNING: $secrets_env is missing issuer or a readable API_KEY_PATH — ASC trio skipped" >&2
    fi
  else
    echo "    WARNING: $secrets_env not found — ASC trio skipped" >&2
  fi

  # --- Android signing + Play service account.
  if [[ -f "$keystore" ]]; then
    base64 -i "$keystore" | gh secret set UPLOAD_KEYSTORE -R "$repo"
    echo "    UPLOAD_KEYSTORE set from $keystore"
  else
    echo "    WARNING: $keystore not found — UPLOAD_KEYSTORE skipped" >&2
  fi
  local key_props="$app_dir/android/key.properties"
  if [[ -f "$key_props" ]]; then
    # Locally storeFile points into ~/creds; in CI the workflow decodes the
    # keystore to android/upload_keystore.jks, which gradle resolves relative
    # to android/app/.
    sed 's|^storeFile=.*|storeFile=../upload_keystore.jks|' "$key_props" |
      base64 | gh secret set KEY_PROPERTIES -R "$repo"
    echo "    KEY_PROPERTIES set from $key_props (storeFile rewritten for CI)"
  else
    echo "    WARNING: $key_props not found — KEY_PROPERTIES skipped" >&2
  fi
  if [[ -f "$play_json" ]]; then
    gh secret set ANDROID_SERVICE_ACCOUNT_JSON -R "$repo" < "$play_json"
    echo "    ANDROID_SERVICE_ACCOUNT_JSON set from $play_json"
  else
    echo "    WARNING: $play_json not found — ANDROID_SERVICE_ACCOUNT_JSON skipped" >&2
  fi

  # --- Drift check: anything outside the known-good set is probably stale.
  local known="APP_STORE_CONNECT_API_KEY_P8 APP_STORE_CONNECT_API_KEY_ID APP_STORE_CONNECT_API_ISSUER_ID UPLOAD_KEYSTORE KEY_PROPERTIES ANDROID_SERVICE_ACCOUNT_JSON $extra_ok"
  local name unknown=""
  while IFS=$'\t' read -r name _; do
    [[ -n "$name" ]] || continue
    if ! grep -qw "$name" <<<"$known"; then
      unknown="$unknown $name"
    fi
  done < <(gh secret list -R "$repo" | tail -n +1)
  if [[ -n "$unknown" ]]; then
    echo "    WARNING: unrecognized secrets in $repo (stale? delete with 'gh secret delete <name> -R $repo'):" >&2
    for name in $unknown; do echo "      - $name" >&2; done
  fi
}

if [[ "${1:-}" == "--all" ]]; then
  sync_app auslan
  sync_app slsl
  sync_app kombio
elif [[ $# -eq 1 ]]; then
  sync_app "$1"
else
  echo "usage: $0 auslan|slsl|kombio | --all" >&2
  exit 1
fi
