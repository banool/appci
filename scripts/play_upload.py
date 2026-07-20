#!/usr/bin/env python3
"""Upload an app bundle to a Play track (the internal track by default).

The counterpart to play_release.py: this puts a *new* build on Play, that one
moves an *already-uploaded* build between tracks. Run this first, then
promote.sh once the build has soaked.

Talks to the Google Play Developer API v3: opens an edit, uploads the .aab,
assigns the resulting versionCode to the target track, and commits. If anything
fails before the commit the edit is discarded, so a failed run changes nothing.

Note the Play API will not create a track that has never been populated through
the Console UI, so the very first upload to a brand-new track is still manual.
Uploading to a track that already has a release — which is the normal case — is
fine.

Self-contained: standard library only. The service-account JWT is signed with
`openssl`, so there are no pip dependencies. Mirrors play_release.py.

Config comes from the environment:
  PLAY_SERVICE_ACCOUNT_JSON_PATH   path to the service-account JSON key
  PLAY_PACKAGE_NAME                Android package, e.g. com.banool.kombio_scorekeeper
  PLAY_AAB_PATH                    path to the .aab (default: the release build output)
  PLAY_TO_TRACK                    target track (default "internal")
  PLAY_RELEASE_NOTES               release notes text (required)
  PLAY_NOTES_LANG                  BCP-47 language for the notes (default "en-US")
  PLAY_COMMIT                      "1" (default) commit | "0" prepare then discard
  PLAY_DRY_RUN                     "1" plan only, upload nothing, discard the edit
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
PLAY_UPLOAD_API = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3"
PLAY_TOKEN_URL = "https://oauth2.googleapis.com/token"
PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

DEFAULT_AAB = "build/app/outputs/bundle/release/app-release.aab"


def die(msg):
    print(f"\n[play_upload] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def env(name):
    value = os.environ.get(name)
    if not value:
        die(f"missing required env var {name}")
    return value


def log(msg):
    print(f"[play_upload] {msg}", flush=True)


def _b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _openssl_sign(key_path, message):
    res = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        input=message,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if res.returncode != 0:
        die("openssl signing failed: " + res.stderr.decode(errors="replace"))
    return res.stdout


def make_jwt(header, payload, sign):
    def compact(obj):
        return _b64url(json.dumps(obj, separators=(",", ":")).encode())

    signing_input = f"{compact(header)}.{compact(payload)}"
    return f"{signing_input}.{_b64url(sign(signing_input.encode()))}"


def http(method, url, *, headers=None, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def play_access_token(key_path):
    """OAuth2 service-account JWT-bearer flow, no client libraries."""
    if not os.path.isfile(key_path):
        die(
            f"Play service account key not found at {key_path}. Drop a JSON key "
            "for the publishing service account there, or set "
            "PLAY_SERVICE_ACCOUNT_JSON_PATH."
        )
    info = json.loads(open(key_path).read())
    now = int(time.time())
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
        f.write(info["private_key"])
        f.flush()
        assertion = make_jwt(
            {"alg": "RS256", "typ": "JWT"},
            {
                "iss": info["client_email"],
                "scope": PLAY_SCOPE,
                "aud": PLAY_TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            },
            lambda msg: _openssl_sign(f.name, msg),
        )
    st, raw = http(
        "POST",
        PLAY_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode(),
    )
    if st != 200:
        die(f"could not get a Play access token (HTTP {st}): {raw.decode(errors='replace')}")
    return json.loads(raw)["access_token"], info.get("client_email", "?")


class Play:
    def __init__(self, token, package):
        self.token = token
        self.package = package

    def call(self, method, path, body=None):
        url = f"{PLAY_API}/applications/{self.package}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        st, raw = http(method, url, headers=headers, body=data)
        return st, _parse(raw)

    def upload_bundle(self, edit_id, aab_path):
        """Simple (non-resumable) media upload of the .aab."""
        url = (
            f"{PLAY_UPLOAD_API}/applications/{self.package}/edits/{edit_id}"
            "/bundles?uploadType=media"
        )
        with open(aab_path, "rb") as f:
            blob = f.read()
        st, raw = http(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(blob)),
            },
            body=blob,
        )
        return st, _parse(raw)


def _parse(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw.decode(errors="replace")}


def err(data, st):
    if isinstance(data, dict):
        e = data.get("error")
        if isinstance(e, dict):
            return f"{e.get('status', st)}: {e.get('message', '')}"
        if "raw" in data:
            return data["raw"][:800]
    return json.dumps(data)[:800] if data is not None else f"HTTP {st}"


def main():
    key_path = os.path.expanduser(env("PLAY_SERVICE_ACCOUNT_JSON_PATH"))
    package = env("PLAY_PACKAGE_NAME")
    aab_path = os.path.expanduser(os.environ.get("PLAY_AAB_PATH", DEFAULT_AAB))
    to_track = os.environ.get("PLAY_TO_TRACK", "internal").strip() or "internal"
    notes = os.environ.get("PLAY_RELEASE_NOTES", "").strip()
    notes_lang = os.environ.get("PLAY_NOTES_LANG", "en-US").strip() or "en-US"
    commit = os.environ.get("PLAY_COMMIT", "1").strip() != "0"
    dry_run = os.environ.get("PLAY_DRY_RUN", "0").strip() == "1"

    if not notes:
        die("PLAY_RELEASE_NOTES is empty — release notes are required")
    if not os.path.isfile(aab_path):
        die(f"no app bundle at {aab_path} — run `flutter build appbundle` first")

    size_mb = os.path.getsize(aab_path) / (1024 * 1024)
    log(f"bundle: {aab_path} ({size_mb:.1f} MiB)")

    # Catch a reused versionCode before pushing 60 MiB over the wire. Skipped on
    # a dry run, which changes nothing and is useful for simply reading back
    # what a track holds. Also skippable for the rare case of uploading a bundle
    # that pubspec no longer describes.
    if not dry_run and os.environ.get("PLAY_SKIP_PREFLIGHT", "0").strip() != "1":
        preflight = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preflight.py")
        if os.path.isfile(preflight):
            rc = subprocess.run([sys.executable, preflight, "--android-only"]).returncode
            if rc != 0:
                die("preflight failed — see above (PLAY_SKIP_PREFLIGHT=1 overrides)")

    if dry_run:
        log("DRY RUN — the edit will be opened, then discarded without uploading")

    token, sa_email = play_access_token(key_path)
    log(f"authenticated as {sa_email}")
    play = Play(token, package)

    # 1. Open an edit.
    st, data = play.call("POST", "/edits")
    if st != 200:
        die(f"could not open a Play edit (HTTP {st}): {err(data, st)}")
    edit_id = data["id"]
    log(f"opened edit {edit_id}")

    def discard():
        s, _ = play.call("DELETE", f"/edits/{edit_id}")
        log(f"discarded edit {edit_id}" if s in (200, 204) else f"(edit {edit_id} left to expire)")

    try:
        # 2. Report what the target track holds now, so a stale build is obvious.
        st, dst = play.call("GET", f"/edits/{edit_id}/tracks/{to_track}")
        if st == 200:
            existing = sorted(
                int(c)
                for r in dst.get("releases") or []
                for c in r.get("versionCodes") or []
            )
            log(f"{to_track} track currently has versionCode(s) {existing or '(none)'}")
        elif st == 404:
            die(
                f"the {to_track} track does not exist yet. The Play API cannot "
                "create a track that has never been populated through the "
                "Console UI — do the first upload there by hand."
            )
        else:
            die(f"could not read the {to_track} track (HTTP {st}): {err(dst, st)}")

        if dry_run:
            log(f"[dry-run] would upload {aab_path} and add it to {to_track}; discarding edit")
            discard()
            return

        # 3. Upload the bundle. Play assigns the versionCode from the manifest.
        log("uploading (this takes a while for a large bundle)...")
        st, data = play.upload_bundle(edit_id, aab_path)
        if st != 200:
            detail = err(data, st)
            low = detail.lower()
            if "already been used" in low or "already used" in low:
                die(
                    f"this versionCode has already been uploaded to Play.\n"
                    "  Commit something (the pre-commit hook bumps the build "
                    "number) and rebuild.\n"
                    f"  detail: {detail}"
                )
            if st == 403:
                die(
                    "HTTP 403 uploading — the service account likely lacks the "
                    "'Release apps to testing tracks' permission in the Play "
                    f"Console.\n  detail: {detail}"
                )
            die(f"could not upload the bundle (HTTP {st}): {detail}")
        version_code = int(data["versionCode"])
        log(f"uploaded — Play assigned versionCode {version_code}")

        # 4. Put it on the track.
        release = {
            "versionCodes": [str(version_code)],
            "releaseNotes": [{"language": notes_lang, "text": notes}],
            "status": "completed",
        }
        st, data = play.call(
            "PUT",
            f"/edits/{edit_id}/tracks/{to_track}",
            body={"track": to_track, "releases": [release]},
        )
        if st != 200:
            die(f"could not write the {to_track} track (HTTP {st}): {err(data, st)}")
        log(f"{to_track} release: versionCode {version_code}, notes[{notes_lang}]")

        if not commit:
            log(f"PLAY_COMMIT=0 — prepared the {to_track} track but discarding (not committing)")
            discard()
            return

        # 5. Commit.
        st, data = play.call("POST", f"/edits/{edit_id}:commit")
        if st != 200:
            die(f"could not commit the edit (HTTP {st}): {err(data, st)}")
        log(f"committed — versionCode {version_code} is on the {to_track} track")

    except SystemExit:
        discard()
        raise
    except Exception:
        discard()
        raise


if __name__ == "__main__":
    main()
