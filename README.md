# appci

Shared release / CI tooling for my three Flutter apps: [auslan_dictionary](https://github.com/banool/auslan_dictionary), [slsl_dictionary](https://github.com/banool/slsl_dictionary) (app in `frontend/`), and kombio_scorekeeper (private). Deliberately NOT a general-purpose tool — it is hardcoded where that makes life easier.

Everything here exists so each app can do three things, both locally and via GitHub Actions:

1. **Release internal builds** — iOS to the internal TestFlight track, Android to the Play internal track.
2. **Promote to beta** — external TestFlight group + Play beta track.
3. **Promote to the public** — App Store + Play production.

No fastlane anywhere: everything is bash + stdlib Python, with JWTs signed via the `openssl` CLI.

## How apps consume this

**Scripts** are resolved via the sibling-checkout convention: clone this repo next to the app repo, or set `APPCI_DIR`. Each app carries thin wrappers that set the app-specific env and exec the canonical script here:

| App wrapper | Canonical script | Env the wrapper sets |
|---|---|---|
| `ios/upload.sh` | `scripts/ios_upload.sh` | `UPLOAD_APP_DIR`, `UPLOAD_BUNDLE_ID` |
| `android/upload.sh` | `scripts/android_upload.sh` | `UPLOAD_APP_DIR`, `PLAY_PACKAGE_NAME`, `PLAY_SERVICE_ACCOUNT_JSON_PATH` |
| `promote.sh` (app root) | `scripts/promote.sh` | `PROMOTE_APP_DIR`, `PROMOTE_BUNDLE_ID`, `PROMOTE_PACKAGE_NAME`, `PROMOTE_BETA_GROUP`, `PLAY_SERVICE_ACCOUNT_JSON_PATH` |
| `screenshots/take_screenshots.py` | `scripts/take_screenshots_lib.py` | n/a — imports the lib and calls `configure(...)` |
| `screenshots/upload_screenshots.py` | `scripts/upload_screenshots_lib.py` | n/a — imports the lib and calls `configure(...)` |
| `integration_test/multi_device/run.sh` (dictionary apps only) | `scripts/multi_device_run.sh` | `MD_APP_DIR`, `MD_BUNDLE_ID`, `MD_ANDROID_PKG`, `MD_APP_ID` |

**Workflows** are consumed as reusable workflows, referenced `@main`:

| Workflow | What | Notes |
|---|---|---|
| `app-format.yaml` | `dart format` gate | |
| `app-release-android.yaml` | test → build appbundle → Play internal via `play_upload.py` | preflight gate before the build; `upload: false` = test-only |
| `app-release-ios.yaml` | archive on macos-15 → TestFlight internal via the app's `ios/upload.sh` | exists because ASC rejects binaries built on prerelease macOS (ITMS-90111) |
| `app-promote.yaml` | run the app's `promote.sh` on ubuntu | iOS promotion is pure ASC API — no macOS needed |
| `app-web-deploy.yaml` | Flutter web → Cloudflare Pages | dictionary apps only |
| `app-pages-deploy.yaml` | static site → Cloudflare Pages | dictionary apps only |

The auslan/slsl callers trigger the release workflows automatically on push (public repos, free minutes); kombio is `workflow_dispatch`-only (private repo, macOS minutes bill at 10x). Callers own concurrency: push-triggered callers use `cancel-in-progress: true`, dispatch upload/promote callers use `cancel-in-progress: false` (never cancel a mid-flight store operation).

`scripts/preflight.py` guards every upload and promote: it refuses version strings the App Store would treat as downgrades, build numbers either store has already seen, and promotes where pubspec has drifted from the uploaded build (a commit between upload and promote strands the build). `PLAY_SKIP_PREFLIGHT=1` / `--no-submit`-style escape hatches exist per script.

## Credentials

Key material lives in `~/creds` (see its README); repo-local gitignored files are thin pointers at it:

- `ios/secrets.env` — `TEAM_ID`, `APP_STORE_CONNECT_API_ISSUER_ID`, `API_KEY_PATH` (→ `~/creds/AuthKey_<ID>.p8`). The key ID is derived from the filename.
- `android/key.properties` — keystore passwords + `storeFile` → `~/creds/<app>_upload_keystore.jks`. Gradle reads this file directly, which is why it stays in-repo.
- Play service-account JSONs: `~/creds/play_<app>.json`, pointed at by the wrappers via `PLAY_SERVICE_ACCOUNT_JSON_PATH`.

**GitHub Actions secrets are a mirror of the local files, never the source of truth.** `scripts/sync_gha_secrets.sh <app>|--all` pushes them up with `gh secret set` and warns about unrecognized (probably stale) secrets. Rotating a credential = update `~/creds`, re-run it. Secrets per app repo:

| Secret | Contents |
|---|---|
| `APP_STORE_CONNECT_API_KEY_P8` | raw PEM of the ASC API key (not base64) |
| `APP_STORE_CONNECT_API_KEY_ID` / `APP_STORE_CONNECT_API_ISSUER_ID` | plain strings |
| `UPLOAD_KEYSTORE` | base64 of the upload keystore |
| `KEY_PROPERTIES` | base64 of `key.properties` with `storeFile=../upload_keystore.jks` (the CI decode location) |
| `ANDROID_SERVICE_ACCOUNT_JSON` | the Play service-account JSON, verbatim |

Apps also carry app-specific secrets outside appci's scope (Cloudflare, R2, Docker Hub) — the sync script knows them as legitimate.

## Version bumping

Each repo has its own `.githooks/pre-commit` + `bump_version.sh` copy-pair (installed with `git config core.hooksPath .githooks`) that unconditionally bumps `version: X.Y.Z+N` on every commit — both stores reject a reused build number, and CI uploads on a broader set of changes than any path filter would catch, so the bump must never be gated. `scripts/bump_version.sh` here is the canonical reference copy; it cannot be shared at runtime because the hook must work in a bare fresh clone.

## Cross-repo couplings to keep in mind

- `poster_name()` in `take_screenshots_lib.py` must keep matching `_posterUrlFor` in dictionarylib's `lib/video_player_screen.dart`.
- The screenshot curation slugs the apps pass as `app_store_shots` / `play_shots` must match what the app's `integration_test/screenshot_test.dart` captures (for the dictionary apps that suite lives in `dictionarylib_test_support`).
- The wire between `app-release-android.yaml`'s decode paths (`android/upload_keystore.jks`, `android/key.properties`) and the `KEY_PROPERTIES` secret's `storeFile` line: change one, change both (and `sync_gha_secrets.sh`).
- Editing a reusable workflow here does NOT trigger the apps' CI — bump the app's `.force` sentinel or `gh workflow run` to exercise the change.
