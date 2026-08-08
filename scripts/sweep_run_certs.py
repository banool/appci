#!/usr/bin/env python3
"""Revoke the signing certificates a CI run created.

xcodebuild's -allowProvisioningUpdates mints a fresh Development certificate
on every run, because a CI runner starts with an empty keychain and so can
never reuse the previous one. The private key dies with the runner, so each
run leaves behind a certificate nobody can ever use again — and Apple caps how
many a team may hold. Left alone they accumulate until archiving fails with
"Your account has reached the maximum number of certificates".

So: snapshot the certificate ids before the build, and afterwards revoke
whatever is new. Two independent conditions must both hold before anything is
revoked, because the team is shared and runs overlap:

  1. the id was absent from the pre-build snapshot, and
  2. the certificate's private key is in THIS runner's keychain.

(1) alone is not enough. Two iOS releases running at once (say auslan and slsl)
each snapshot the same "before" set and each then mint a certificate, so each
sees the other's as new — and a sweep on that basis would revoke a certificate
the other run is still signing with. (2) settles ownership: a certificate whose
key is on this machine is one this run created, since a CI runner starts with
an empty keychain and Apple hands the key out exactly once, at creation.

  sweep_run_certs.py snapshot <file>   write the current ids to <file>
  sweep_run_certs.py sweep <file>      revoke ids that appeared since <file>
                                       AND have a private key here

Needs APP_STORE_CONNECT_API_KEY_ID, APP_STORE_CONNECT_API_ISSUER_ID and
API_KEY_PATH, the same trio the other scripts here use.

Never fails the build: a cleanup that breaks a green release is worse than a
certificate left lying around. Problems are reported and exit 0. The one
exception is `snapshot`, where a missing file would make a later sweep look
like "everything is new" — that case writes nothing and the sweep no-ops.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.appstoreconnect.apple.com"


def log(msg):
    print(f"[sweep_run_certs] {msg}", flush=True)


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _der_to_raw(der):
    # openssl emits an ECDSA signature as DER (SEQUENCE of two INTEGERs); JOSE
    # (ES256) wants raw r||s, 32 bytes each. P-256 sigs are short, so all the
    # ASN.1 lengths are single-byte (short form).
    if not der or der[0] != 0x30:
        raise ValueError("unexpected signature encoding from openssl")
    i = 2  # skip SEQUENCE tag + length byte
    if der[i] != 0x02:
        raise ValueError("bad signature (expected INTEGER for r)")
    rlen = der[i + 1]
    r = der[i + 2 : i + 2 + rlen]
    i = i + 2 + rlen
    if der[i] != 0x02:
        raise ValueError("bad signature (expected INTEGER for s)")
    slen = der[i + 1]
    s = der[i + 2 : i + 2 + slen]
    r = r.lstrip(b"\x00").rjust(32, b"\x00")
    s = s.lstrip(b"\x00").rjust(32, b"\x00")
    return r + s


class Client:
    def __init__(self):
        self.key_id = os.environ["APP_STORE_CONNECT_API_KEY_ID"]
        self.issuer = os.environ["APP_STORE_CONNECT_API_ISSUER_ID"]
        self.key_path = os.path.expandvars(os.environ["API_KEY_PATH"])

    def token(self):
        now = int(time.time())
        header = {"alg": "ES256", "kid": self.key_id, "typ": "JWT"}
        payload = {
            "iss": self.issuer,
            "iat": now,
            "exp": now + 1200,
            "aud": "appstoreconnect-v1",
        }
        signing_input = (
            b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + b64url(json.dumps(payload, separators=(",", ":")).encode())
        )
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", self.key_path],
            input=signing_input.encode(),
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "openssl signing failed: " + proc.stderr.decode(errors="replace")
            )
        return signing_input + "." + b64url(_der_to_raw(proc.stdout))

    def call(self, method, path, params=None):
        url = path if path.startswith("http") else API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", "Bearer " + self.token())
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"errors": [{"detail": raw.decode(errors="replace")}]}
            return exc.code, parsed

    def certificates(self):
        """Every certificate on the team as {id: attributes}, following pagination."""
        certs = {}
        status, data = self.call("GET", "/v1/certificates", params={"limit": 200})
        while True:
            if status != 200:
                raise RuntimeError(f"listing certificates failed: {status} {data}")
            certs.update((c["id"], c.get("attributes") or {}) for c in data.get("data", []))
            nxt = (data.get("links") or {}).get("next")
            if not nxt:
                return certs
            status, data = self.call("GET", nxt)


def cert_sha1(attrs):
    """SHA-1 of a certificate's DER, or None if the API didn't return its bytes.

    This is the same fingerprint `security find-identity` prints, so it's what
    lets us match an App Store Connect certificate to a local keychain entry.
    """
    content = attrs.get("certificateContent")
    if not content:
        return None
    try:
        return hashlib.sha1(base64.b64decode(content)).hexdigest().upper()
    except Exception:
        return None


def keychain_identity_sha1s():
    """Fingerprints of every codesigning IDENTITY in the runner's keychain.

    An identity is a certificate paired with its private key, so this is
    exactly the set of certificates this machine could have created — Apple
    releases the key once, at creation, and a CI runner starts empty.
    """
    try:
        proc = subprocess.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run security find-identity: {exc}")
    if proc.returncode != 0:
        raise RuntimeError(
            "security find-identity failed: " + proc.stderr.strip()
        )
    return {h.upper() for h in re.findall(r"\b([0-9A-Fa-f]{40})\b", proc.stdout)}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("snapshot", "sweep"):
        print(__doc__, file=sys.stderr)
        return 2
    mode, path = sys.argv[1], sys.argv[2]

    try:
        client = Client()
        current = client.certificates()
    except Exception as exc:
        log(f"skipping ({exc})")
        return 0

    if mode == "snapshot":
        try:
            with open(path, "w") as fh:
                json.dump(sorted(current), fh)
            log(f"recorded {len(current)} existing certificate(s)")
        except OSError as exc:
            log(f"could not write the snapshot, the sweep will no-op ({exc})")
        return 0

    try:
        with open(path) as fh:
            before = set(json.load(fh))
    except (OSError, ValueError) as exc:
        log(f"no usable snapshot, leaving every certificate alone ({exc})")
        return 0

    appeared = sorted(set(current) - before)
    if not appeared:
        log("no new certificates since the snapshot, nothing to revoke")
        return 0

    try:
        local = keychain_identity_sha1s()
    except RuntimeError as exc:
        log(f"could not read the keychain, leaving every certificate alone ({exc})")
        return 0

    created = [c for c in appeared if (cert_sha1(current[c]) or "") in local]
    others = len(appeared) - len(created)
    if others:
        log(
            f"leaving {others} new certificate(s) alone — no private key here, "
            "so they belong to a concurrent run"
        )
    if not created:
        log("this run created no certificates, nothing to revoke")
        return 0

    log(f"revoking {len(created)} certificate(s) created by this run")
    for cert_id in created:
        status, data = client.call("DELETE", f"/v1/certificates/{cert_id}")
        if status in (200, 204):
            log(f"  revoked {cert_id}")
        else:
            log(f"  could not revoke {cert_id}: {status} {data}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
