"""Regression coverage for issue #2 without paid network calls."""
import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
import requests

from conftest import KEY_A, SERVER
from lib import auth_request, session as sessions
from lib.serialize import serialize_response
from test_session_identity import _session
import cli


def response(status=200):
    r = requests.Response()
    r.status_code = status
    r._content = b'{"ok":true}'
    r.headers.update({
        "Content-Type": "application/json; charset=utf-8",
        "x-bsv-price": "7",
        "x-bsv-auth-request-id": base64.b64encode(b'r' * 32).decode(),
        "x-bsv-auth-identity-key": _session(KEY_A).server_identity_key,
        "x-bsv-auth-your-nonce": _session(KEY_A).client_nonce_b64,
        "x-bsv-auth-nonce": base64.b64encode(b'n' * 32).decode(),
    })
    payload = serialize_response(b'r' * 32, status, [("x-bsv-price", "7")], r.content)
    r.headers["x-bsv-auth-signature"] = hashlib.sha256(payload).hexdigest()
    return r


@pytest.fixture
def verifier(monkeypatch):
    calls = []
    def verify(method, params):
        calls.append(params)
        assert method == "verifySignature"
        return {"valid": hashlib.sha256(bytes(params["data"])).digest() == bytes(params["signature"])}
    monkeypatch.setattr(auth_request, "_call", verify)
    return calls


@pytest.mark.parametrize("status", [200, 400, 402, 500])
def test_signed_statuses(verifier, status):
    assert auth_request.verify_response(response(status), _session(KEY_A), b'r' * 32) == "signed"
    assert verifier[0]["counterparty"] == _session(KEY_A).server_identity_key
    assert verifier[0]["keyID"].endswith(" " + _session(KEY_A).client_nonce_b64)


@pytest.mark.parametrize("tamper", ["body", "status", "header", "signature", "identity", "request", "nonce"])
def test_tampered_response(verifier, tamper):
    r = response()
    if tamper == "body":
        r._content += b'x'
    elif tamper == "status":
        r.status_code = 500
    elif tamper == "header":
        r.headers["x-bsv-price"] = "8"
    else:
        header = {"signature": "signature", "identity": "identity-key",
                  "request": "request-id", "nonce": "your-nonce"}[tamper]
        r.headers["x-bsv-auth-" + header] = "invalid"
    assert auth_request.verify_response(r, _session(KEY_A), b'r' * 32) == "BAD SIGNATURE"


def test_missing_and_wallet_failure(verifier, monkeypatch):
    r = response()
    del r.headers["x-bsv-auth-signature"]
    assert auth_request.verify_response(r, _session(KEY_A), b'r' * 32) == "UNSIGNED"
    def unavailable(*args):
        raise RuntimeError("private wallet detail")
    monkeypatch.setattr(auth_request, "_call", unavailable)
    assert auth_request.verify_response(response(), _session(KEY_A), b'r' * 32) == "BAD SIGNATURE"


def test_request_attaches_verdict_and_warns(net, capsys):
    r = auth_request.authenticated_request("GET", SERVER + "/free")
    assert r.signature_verdict == "UNSIGNED"
    assert "200 UNSIGNED" in capsys.readouterr().err


def helper():
    path = Path(__file__).parents[1] / "skills/x402/scripts/brc31_helpers.py"
    spec = importlib.util.spec_from_file_location("helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_missing_url(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["helper", "session"])
    with pytest.raises(SystemExit) as e:
        helper().main()
    assert e.value.code == 1
    output = capsys.readouterr()
    assert "Provide a server URL" in output.err
    assert "Traceback" not in output.err


def test_clear_all_dry_then_delete(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sessions, "SESSION_DIR", tmp_path)
    (tmp_path / "one.json").write_text("{}")
    (tmp_path / "two.json").write_text("{}")
    (tmp_path / "registry.json").write_text("{}")
    module = helper()
    monkeypatch.setattr(sys, "argv", ["helper", "session", "--clear-all", "--dry-run"])
    with pytest.raises(SystemExit) as e:
        module.main()
    assert e.value.code == 0
    assert "one.json" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*.json"))) == 3
    monkeypatch.setattr(sys, "argv", ["helper", "session", "--clear-all"])
    with pytest.raises(SystemExit) as e:
        module.main()
    assert e.value.code == 0
    assert "Cleared 2" in capsys.readouterr().out
    assert [p.name for p in tmp_path.iterdir()] == ["registry.json"]


def test_unverified_challenge_never_pays(monkeypatch):
    from lib import payment
    r = response(402)
    r.signature_verdict = "BAD SIGNATURE"
    monkeypatch.setattr(payment, "authenticated_request", lambda **kw: r)
    assert payment.paid_request("GET", SERVER + "/paid") is r


@pytest.mark.parametrize("command", ["auth", "pay"])
def test_script_verdict_beside_status(monkeypatch, capsys, command):
    module = helper()
    r = response()
    r.signature_verdict = "signed"
    monkeypatch.setattr(module, "authenticated_request" if command == "auth" else "paid_request", lambda *a, **kw: r)
    monkeypatch.setattr(sys, "argv", ["helper", command, "GET", SERVER + "/free"])
    module.main()
    import json
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == 200
    assert result["verdict"] == "signed"


@pytest.mark.parametrize("command", ["auth", "pay"])
def test_cli_status_verdict_and_failure(monkeypatch, capsys, command):
    r = response()
    r.signature_verdict = "BAD SIGNATURE"
    monkeypatch.setattr(cli, "authenticated_request" if command == "auth" else "paid_request", lambda **kw: r)
    args = cli.build_parser().parse_args([command, "GET", SERVER + "/free"])
    assert getattr(cli, "cmd_" + command)(args) == 1
    assert "200 BAD SIGNATURE" in capsys.readouterr().out


def test_signed_request_integration(net, monkeypatch, verifier):
    def request(method, url, headers, **kw):
        r = response(500)
        rid = base64.b64decode(headers["x-bsv-auth-request-id"])
        r.headers["x-bsv-auth-request-id"] = headers["x-bsv-auth-request-id"]
        payload = serialize_response(rid, 500, [("x-bsv-price", "7")], r.content)
        r.headers["x-bsv-auth-signature"] = hashlib.sha256(payload).hexdigest()
        return r
    monkeypatch.setattr(requests, "request", request)
    r = auth_request.authenticated_request("GET", SERVER + "/free", session=_session(KEY_A))
    assert r.status_code == 500
    assert r.signature_verdict == "signed"
