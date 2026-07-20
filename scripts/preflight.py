#!/usr/bin/env python3
"""Refuse to ship a version the stores will reject.

Run before building or promoting. It compares the version in pubspec.yaml
against what each store already knows about, and exits non-zero if the release
cannot succeed. Every check here corresponds to a mistake that has actually
happened to one of these apps:

  1. Version string not greater than the App Store's highest.
     App Store Connect orders version strings component-wise and numerically,
     so 1.10.0 is LOWER than 19.17. A stray "19.17" (almost certainly a typo
     for 1.9.17, created 2026-02-19) means every 1.x and 2.x release is
     rejected as a downgrade. This is the check that would have caught it.

  2. Build number already used. Both stores refuse a reused build
     number/versionCode, and on iOS the failure arrives late, after a full
     archive and upload.

  3. Marketing version drift between pubspec and the build being promoted.
     App Store Connect only attaches a build whose CFBundleShortVersionString
     matches the App Store version, so a commit between building and promoting
     silently invalidates the build.

Self-contained: standard library only, JWTs signed with `openssl`, matching the
other scripts here.

Config comes from the environment (the same vars the sibling scripts use):
  APP_STORE_CONNECT_API_KEY_ID / _ISSUER_ID / API_KEY_PATH   iOS
  ASC_BUNDLE_ID                                              iOS bundle id
  PLAY_SERVICE_ACCOUNT_JSON_PATH / PLAY_PACKAGE_NAME         Android

Flags:
  --ios-only / --android-only   restrict to one store
  --promote                     check before promoting rather than before
                                building: the build number must now MATCH what
                                was uploaded (and, on iOS, the uploaded build's
                                marketing version must match pubspec) instead of
                                exceeding it
  --quiet                       only print on failure
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ASC_API = "https://api.appstoreconnect.apple.com"
PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
PLAY_TOKEN_URL = "https://oauth2.googleapis.com/token"
PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

FAILURES = []
NOTES = []


def die(msg):
    print(f"\n[preflight] ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def env(name):
    value = os.environ.get(name)
    if not value:
        die(f"missing required env var {name}")
    return value


def log(msg):
    if not QUIET:
        print(f"[preflight] {msg}", flush=True)


def fail(msg):
    FAILURES.append(msg)


def note(msg):
    NOTES.append(msg)


# --- version comparison -----------------------------------------------------


def parse_version(s):
    """"1.10.0" -> (1, 10, 0). Raises on anything not purely numeric-dotted."""
    parts = s.strip().split(".")
    if not all(re.fullmatch(r"\d+", p) for p in parts):
        raise ValueError(f"non-numeric version string {s!r}")
    return tuple(int(p) for p in parts)


def version_cmp(a, b):
    """Compare like the App Store does: component-wise, numerically, shorter
    version zero-padded. Returns -1, 0 or 1."""
    va, vb = parse_version(a), parse_version(b)
    n = max(len(va), len(vb))
    va = va + (0,) * (n - len(va))
    vb = vb + (0,) * (n - len(vb))
    return (va > vb) - (va < vb)


def read_pubspec(path="pubspec.yaml"):
    with open(path) as f:
        for line in f:
            m = re.match(r"^version:\s*(\d+\.\d+\.\d+)\+(\d+)\s*$", line)
            if m:
                return m.group(1), int(m.group(2))
    die("could not find a 'version: X.Y.Z+N' line in pubspec.yaml")


# --- shared HTTP ------------------------------------------------------------


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def http(method, url, headers=None, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw.decode(errors="replace")}


# --- App Store Connect ------------------------------------------------------


def _der_to_raw(der):
    # openssl emits ECDSA as DER (SEQUENCE of two INTEGERs); JOSE wants raw r||s.
    if not der or der[0] != 0x30:
        die("unexpected signature encoding from openssl")
    i = 2
    if der[i] != 0x02:
        die("bad signature (expected INTEGER for r)")
    rlen = der[i + 1]
    r = der[i + 2 : i + 2 + rlen]
    i = i + 2 + rlen
    if der[i] != 0x02:
        die("bad signature (expected INTEGER for s)")
    slen = der[i + 1]
    s = der[i + 2 : i + 2 + slen]
    return r.lstrip(b"\x00").rjust(32, b"\x00") + s.lstrip(b"\x00").rjust(32, b"\x00")


def asc_token():
    key_id = env("APP_STORE_CONNECT_API_KEY_ID")
    issuer = env("APP_STORE_CONNECT_API_ISSUER_ID")
    key_path = os.path.expanduser(env("API_KEY_PATH"))
    now = int(time.time())
    signing_input = (
        b64url(json.dumps({"alg": "ES256", "kid": key_id, "typ": "JWT"}, separators=(",", ":")).encode())
        + "."
        + b64url(
            json.dumps(
                {"iss": issuer, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
                separators=(",", ":"),
            ).encode()
        )
    )
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path],
        input=signing_input.encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        die("openssl signing failed: " + proc.stderr.decode(errors="replace"))
    return signing_input + "." + b64url(_der_to_raw(proc.stdout))


def asc_get(token, path, params=None):
    url = ASC_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    st, data = http("GET", url, {"Authorization": "Bearer " + token})
    if st != 200:
        die(f"App Store Connect GET {path} failed (HTTP {st}): {json.dumps(data)[:400]}")
    return data


def check_ios(version, build):
    bundle_id = env("ASC_BUNDLE_ID")
    token = asc_token()

    apps = asc_get(token, "/v1/apps", {"filter[bundleId]": bundle_id, "limit": 1})
    if not apps.get("data"):
        die(f"no app found for bundle id {bundle_id}")
    app_id = apps["data"][0]["id"]
    log(f"iOS: app {bundle_id} -> {app_id}")

    # 1. Version string must beat every version the App Store already has.
    versions = asc_get(
        token, f"/v1/apps/{app_id}/appStoreVersions", {"limit": 200}
    ).get("data", [])
    seen = []
    for v in versions:
        vs = v["attributes"]["versionString"]
        try:
            parse_version(vs)
        except ValueError:
            note(f"iOS: ignoring unparseable existing version {vs!r}")
            continue
        seen.append((vs, v["attributes"].get("appStoreState")))
    if seen:
        highest, state = max(seen, key=lambda p: parse_version(p[0]))
        log(f"iOS: highest existing App Store version is {highest} ({state})")
        c = version_cmp(version, highest)
        if c > 0:
            log(f"iOS: {version} > {highest} — OK")
        elif c == 0 and PROMOTE:
            # Expected: promoting creates (or renames into) this version, so on
            # a retry it is already there. Only a version that has moved beyond
            # our control would matter, and appstore_release.py checks that.
            log(f"iOS: version {version} already exists ({state}) — expected when promoting")
        elif c == 0:
            fail(
                f"iOS: version {version} already exists on App Store Connect "
                f"(state {state}). Bump the version in pubspec.yaml."
            )
        else:
            fail(
                f"iOS: version {version} is LOWER than the existing {highest} "
                f"({state}). App Store Connect compares version strings "
                f"component-wise and numerically, so this is a downgrade and "
                f"will be rejected. Pick a version greater than {highest}."
            )

    # 2. Build number. Before a build the bar is "must be unused"; before a
    #    promote it is the opposite — pubspec must still describe the build
    #    that was actually uploaded. A commit between building and promoting
    #    bumps pubspec and silently invalidates the build, which is exactly how
    #    build 268 got stranded at 1.9.41 while pubspec had moved to 1.10.0.
    builds = asc_get(
        token,
        "/v1/builds",
        {"filter[app]": app_id, "limit": 200, "sort": "-uploadedDate"},
    ).get("data", [])
    nums = []
    for b in builds:
        try:
            nums.append((int(b["attributes"]["version"]), b["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not nums:
        return
    top, top_id = max(nums, key=lambda p: p[0])

    if not PROMOTE:
        if build > top:
            log(f"iOS: build {build} > highest uploaded {top} — OK")
        else:
            fail(
                f"iOS: build number {build} is not greater than the highest "
                f"already uploaded ({top}). Commit (the pre-commit hook bumps "
                f"it) and rebuild."
            )
        return

    if build != top:
        fail(
            f"iOS: pubspec build number is {build} but the latest uploaded "
            f"build is {top}. Promoting would ship the wrong build, or fail "
            f"outright. Rebuild and re-upload, or check out the commit that "
            f"produced build {top}."
        )
        return
    log(f"iOS: pubspec build {build} matches the latest uploaded build — OK")

    # The build's own marketing version has to match too: App Store Connect
    # only attaches a build whose CFBundleShortVersionString equals the App
    # Store version being created.
    pre = asc_get(token, f"/v1/builds/{top_id}/preReleaseVersion").get("data") or {}
    marketing = (pre.get("attributes") or {}).get("version")
    if not marketing:
        note(f"iOS: could not read build {top}'s marketing version — skipping that check")
    elif version_cmp(marketing, version) != 0:
        fail(
            f"iOS: build {top} was built as version {marketing}, but pubspec "
            f"now says {version}. App Store Connect only attaches a build whose "
            f"version matches, so this promote would fail. Rebuild at {version}."
        )
    else:
        log(f"iOS: build {top} marketing version {marketing} matches pubspec — OK")


# --- Google Play ------------------------------------------------------------


def play_token(key_path):
    if not os.path.isfile(key_path):
        die(f"Play service account key not found at {key_path}")
    info = json.loads(open(key_path).read())
    now = int(time.time())
    signing_input = (
        b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        + "."
        + b64url(
            json.dumps(
                {
                    "iss": info["client_email"],
                    "scope": PLAY_SCOPE,
                    "aud": PLAY_TOKEN_URL,
                    "iat": now,
                    "exp": now + 3600,
                },
                separators=(",", ":"),
            ).encode()
        )
    )
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
        f.write(info["private_key"])
        f.flush()
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", f.name],
            input=signing_input.encode(),
            capture_output=True,
        )
    if proc.returncode != 0:
        die("openssl signing failed: " + proc.stderr.decode(errors="replace"))
    assertion = signing_input + "." + b64url(proc.stdout)
    st, data = http(
        "POST",
        PLAY_TOKEN_URL,
        {"Content-Type": "application/x-www-form-urlencoded"},
        urllib.parse.urlencode(
            {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        ).encode(),
    )
    if st != 200:
        die(f"could not get a Play access token (HTTP {st}): {json.dumps(data)[:400]}")
    return data["access_token"]


def check_android(version, build):
    key_path = os.path.expanduser(env("PLAY_SERVICE_ACCOUNT_JSON_PATH"))
    package = env("PLAY_PACKAGE_NAME")
    token = play_token(key_path)
    hdr = {"Authorization": "Bearer " + token}

    st, data = http("POST", f"{PLAY_API}/applications/{package}/edits", hdr)
    if st != 200:
        die(f"could not open a Play edit (HTTP {st}): {json.dumps(data)[:400]}")
    edit_id = data["id"]
    try:
        st, data = http("GET", f"{PLAY_API}/applications/{package}/edits/{edit_id}/bundles", hdr)
        if st != 200:
            die(f"could not list Play bundles (HTTP {st}): {json.dumps(data)[:400]}")
        codes = [int(b["versionCode"]) for b in data.get("bundles", []) if "versionCode" in b]
        if not codes:
            note("Android: no bundles uploaded yet — skipping the versionCode check")
        elif PROMOTE:
            top = max(codes)
            if build == top:
                log(f"Android: pubspec versionCode {build} matches the latest uploaded — OK")
            else:
                fail(
                    f"Android: pubspec versionCode is {build} but the latest "
                    f"uploaded bundle is {top}. Promoting would ship a different "
                    f"build than pubspec describes."
                )
        else:
            top = max(codes)
            if build > top:
                log(f"Android: versionCode {build} > highest uploaded {top} — OK")
            else:
                fail(
                    f"Android: versionCode {build} has already been uploaded "
                    f"(highest is {top}). Play permanently refuses a reused "
                    f"versionCode. Commit and rebuild."
                )
    finally:
        http("DELETE", f"{PLAY_API}/applications/{package}/edits/{edit_id}", hdr)

    # Play does not order versionName, so a downgrade there is silent rather
    # than fatal. Still worth saying out loud: divergence between the stores'
    # user-visible versions is what made the 19.17 typo so hard to spot.
    note(
        f"Android: Play does not enforce versionName ordering, so {version} is "
        f"accepted regardless. Keep it in step with iOS deliberately."
    )


# --- main -------------------------------------------------------------------


def main():
    global QUIET, PROMOTE
    args = sys.argv[1:]
    QUIET = "--quiet" in args
    PROMOTE = "--promote" in args
    ios = "--android-only" not in args
    android = "--ios-only" not in args

    version, build = read_pubspec()
    log(f"pubspec version {version} (build {build}){' [promote mode]' if PROMOTE else ''}")

    if ios:
        check_ios(version, build)
    if android:
        check_android(version, build)

    for n in NOTES:
        log(n)

    if FAILURES:
        print("\n[preflight] RELEASE BLOCKED:", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)

    log("all checks passed")


if __name__ == "__main__":
    QUIET = False
    PROMOTE = False
    main()
