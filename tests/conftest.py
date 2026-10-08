"""Shared fixtures: an in-memory fake wallet and BRC-31 server.

No real wallet or network is touched. ``requests.post`` (wallet + handshake)
and ``requests.request`` (general messages) are replaced with fakes that route
by URL.
"""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills", "x402"))

import requests  # noqa: E402

from lib import metanet, session as session_mod  # noqa: E402

KEY_A = "02" + "aa" * 32
KEY_B = "03" + "bb" * 32
SERVER_KEY = "02" + "5e" * 32
SERVER = "https://api.example.test"


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self.reason = "OK" if self.ok else "ERR"
        self._body = body
        self.text = json.dumps(body) if body is not None else ""
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeNet:
    """Routes wallet and server traffic; records every call."""

    def __init__(self):
        # wallet base URL -> identity key it reports
        self.wallets = {"http://localhost:3321": KEY_A, "http://localhost:3322": KEY_B}
        self.wallet_calls = []      # (url, params)
        self.handshakes = []        # identityKey sent in each initialRequest
        self.general = []           # (url, x-bsv-auth-identity-key)
        self.general_status = 200
        self._nonce = 0

    def post(self, url, headers=None, json=None, data=None, timeout=None):
        for base, key in self.wallets.items():
            if url.startswith(base + "/"):
                self.wallet_calls.append((url, json))
                method = url[len(base) + 1:]
                if method == "getPublicKey":
                    return FakeResponse(body={"publicKey": key})
                if method == "createSignature":
                    return FakeResponse(body={"signature": [0x30, 0x06, 1, 2, 3, 4]})
                return FakeResponse(404, {"error": f"unknown {method}"})
        if url.endswith("/.well-known/auth"):
            req = __import__("json").loads(data)
            self.handshakes.append(req["identityKey"])
            self._nonce += 1
            server_nonce = base64.b64encode(bytes([self._nonce]) * 32).decode()
            return FakeResponse(body={
                "identityKey": SERVER_KEY,
                "initialNonce": server_nonce,
                "yourNonce": req["initialNonce"],
            })
        raise requests.ConnectionError(f"unexpected POST {url}")

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.general.append((url, headers.get("x-bsv-auth-identity-key")))
        return FakeResponse(self.general_status, {"ok": self.general_status == 200})


@pytest.fixture
def net(monkeypatch, tmp_path):
    fake = FakeNet()
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "request", fake.request)
    monkeypatch.setattr(session_mod, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.delenv("X402_WALLET_URL", raising=False)
    monkeypatch.delenv("METANET_URL", raising=False)
    metanet.reset_identity_cache()
    yield fake
    metanet.reset_identity_cache()


@pytest.fixture
def use_wallet(monkeypatch):
    """Point the helper at a wallet URL, as a new process would."""
    def _use(url):
        monkeypatch.setenv("X402_WALLET_URL", url)
        metanet.reset_identity_cache()
    return _use
