"""End-to-end UHRP file upload over BRC-31 auth + BRC-29 payment.

UHRP storage servers (nanostore.babbage.systems and protocol-compatible
ports such as bsv-storage-cloudflare) split a file upload into two steps:

  1. POST /upload {fileSize, retentionPeriod}  -> paid (BRC-29). The server
     replies with a *presigned* storage URL (GCS or R2 S3) plus the
     ``requiredHeaders`` that must accompany the PUT. The server never
     proxies the file bytes.
  2. PUT the raw bytes directly to that presigned URL.

The ``pay`` command only does step 1, so callers had to hand-roll the PUT
and then guess the public URL. ``do_upload`` does the whole thing and
returns the public, browsable URL.

Works against any protocol-compatible UHRP server:

* nanostore -> files served from a public GCS bucket; the public URL is the
  presigned URL with its query stripped (and matches the discovery
  manifest's ``publicUrlFormat``).
* bsv-storage-cloudflare (R2) -> the presigned PUT goes to the R2 *S3 API*
  endpoint (``<acct>.r2.cloudflarestorage.com``), which is NOT the public
  domain. R2's public domain (an ``r2.dev`` URL or a custom domain) is
  separate, so it can't be derived from the upload URL. Pass it with
  ``public_base`` to get the browsable URL.
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse

import requests

from lib import registry
from lib.payment import paid_request

# A year, in minutes (UHRP retention is expressed in minutes; 525600 = 365d).
DEFAULT_RETENTION_MINUTES = 525_600


class UploadError(Exception):
    """Raised when any step of the UHRP upload fails."""


def _guess_content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


def _fetch_manifest(server: str) -> dict | None:
    """Best-effort fetch of the server's x402 discovery manifest.

    Used only to read ``publicUrlFormat`` for public-URL derivation. Servers
    that gate or omit discovery (e.g. an R2 worker that 401s ``/.well-known``)
    simply yield ``None`` and we fall back to other derivation rules.
    """
    try:
        info_url = registry.resolve_x402_info(server)
        resp = requests.get(info_url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _object_key(upload_url: str) -> str:
    """Extract the storage object key (``cdn/<id>``) from a presigned URL.

    The presigned path is ``/<bucket>/<key...>``; the key is everything after
    the first (bucket) segment. Matches both GCS (``/prod-uhrp/cdn/<id>``) and
    R2 (``/uhrp-prod/cdn/<id>``).
    """
    path = urllib.parse.urlparse(upload_url).path.lstrip("/")
    parts = path.split("/", 1)
    return parts[1] if len(parts) > 1 else path


def _derive_public_url(
    upload_url: str, object_key: str, public_base: str | None, manifest: dict | None
) -> str | None:
    # 1. Explicit override always wins.
    if public_base:
        return public_base.rstrip("/") + "/" + object_key
    # 2. Discovery manifest's publicUrlFormat (nanostore advertises this).
    if manifest:
        pattern = (manifest.get("publicUrlFormat") or {}).get("pattern")
        if pattern and "{base58id}" in pattern:
            return pattern.replace("{base58id}", object_key.rsplit("/", 1)[-1])
    # 3. Strip the query off the presigned URL. Correct when the storage
    #    endpoint IS the public domain (GCS public buckets / nanostore).
    parsed = urllib.parse.urlparse(upload_url)
    if "r2.cloudflarestorage.com" in parsed.netloc:
        # R2's S3 API host is never public — caller must supply public_base.
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def do_upload(
    file_path: str,
    server: str,
    retention_minutes: int = DEFAULT_RETENTION_MINUTES,
    public_base: str | None = None,
    verify: bool = True,
) -> dict:
    """Upload ``file_path`` to a UHRP storage ``server`` and return its URL.

    ``server`` may be a registry name (e.g. ``nanostore``) or a full URL.
    Returns a dict with ``publicURL`` (or ``None`` + a ``note`` when it can't
    be derived), the ``objectKey``, sats paid, and a verify result.
    """
    if not os.path.isfile(file_path):
        raise UploadError(f"file not found: {file_path}")

    data = open(file_path, "rb").read()
    file_size = len(data)
    base = registry.resolve(server).rstrip("/")
    content_type = _guess_content_type(file_path)

    # ── Step 1: paid POST /upload (compact JSON for BRC-103/104 signing) ──────
    body = json.dumps(
        {"fileSize": file_size, "retentionPeriod": int(retention_minutes)},
        separators=(",", ":"),
    )
    resp = paid_request("POST", f"{base}/upload", body=body)

    if resp.status_code == 413:
        raise UploadError(
            "413 from /upload: the BRC-29 payment (its BEEF) exceeds this "
            "server's request/header size limit. This happens when the paying "
            "wallet funds from coins with heavy ancestry (large proof BEEFs). "
            "Fix: pay from a wallet with lighter UTXOs, or use a server that "
            "accepts large requests / advertises BRC-105 multipart transport. "
            "(Cloudflare-hosted UHRP servers accept large headers; some "
            "GCS-fronted ones cap near 8KB.)"
        )
    if resp.status_code != 200:
        raise UploadError(f"/upload failed: HTTP {resp.status_code}: {resp.text[:400]}")

    try:
        j = resp.json()
    except Exception as exc:
        raise UploadError(f"/upload returned non-JSON: {resp.text[:200]}") from exc

    upload_url = j.get("uploadURL")
    if not upload_url:
        raise UploadError(f"/upload response missing uploadURL: {json.dumps(j)[:300]}")
    required_headers = j.get("requiredHeaders") or {}
    amount = j.get("amount")
    object_key = _object_key(upload_url)

    # ── Step 2: PUT the bytes straight to the presigned storage URL ───────────
    # requiredHeaders are part of the storage signature and MUST be sent
    # verbatim. Content-Type is unsigned but sets the served MIME type.
    put_headers = dict(required_headers)
    put_headers["Content-Type"] = content_type
    put = requests.put(upload_url, data=data, headers=put_headers, timeout=180)
    if put.status_code not in (200, 201):
        raise UploadError(
            f"PUT to storage failed: HTTP {put.status_code}: {put.text[:400]}"
        )

    # ── Step 3: derive (and optionally verify) the public URL ─────────────────
    manifest = _fetch_manifest(server)
    public_url = _derive_public_url(upload_url, object_key, public_base, manifest)

    result: dict = {
        "status": "success",
        "publicURL": public_url,
        "objectKey": object_key,
        "amountSats": amount,
        "fileSize": file_size,
        "contentType": content_type,
        "retentionMinutes": int(retention_minutes),
        "storageHost": urllib.parse.urlparse(upload_url).netloc,
    }

    if public_url is None:
        result["note"] = (
            "Uploaded, but the public URL can't be auto-derived: this server "
            "stores to an R2 S3 endpoint whose public domain is separate. "
            "Re-run with --public-base <https://your-r2.dev-or-custom-domain> "
            f"to print the browsable URL. Object key: {object_key}"
        )
    elif verify:
        try:
            g = requests.get(public_url, timeout=25)
            result["verified"] = g.status_code == 200
            result["verifyStatus"] = g.status_code
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            result["verified"] = False
            result["verifyError"] = str(exc)

    return result
