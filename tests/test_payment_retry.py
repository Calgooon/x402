"""Lost paid responses reuse one transaction and persist recoverable requests."""
import base64
import hashlib
import json
import shlex
import sys

import pytest
import requests

from conftest import SERVER_KEY
from lib import auth_request, payment
from lib.headers import filter_signable_headers
from lib.serialize import serialize_response
from test_helper_gaps import helper

# Synthetic wallet transaction identifier, never a live payment.
TXID = "12" * 32
URL = "https://service.example.test/paid"


@pytest.fixture
def server(net, monkeypatch, tmp_path):
    monkeypatch.setattr(payment, "PENDING_DIR", tmp_path / "pending")
    now = [1000.0]
    sleeps = []
    monkeypatch.setattr(payment.time, "time", lambda: now[0])

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(payment.time, "sleep", sleep)
    actions = []
    original_post = net.post

    def post(url, **kwargs):
        if url.endswith("/createAction"):
            actions.append(kwargs["json"])
            from conftest import FakeResponse
            return FakeResponse(body={"txid": TXID, "tx": [1, 2, 3]})
        return original_post(url, **kwargs)

    monkeypatch.setattr(requests, "post", post)

    def verify(method, params):
        assert method == "verifySignature"
        return {"valid": hashlib.sha256(bytes(params["data"])).digest() == bytes(params["signature"])}

    monkeypatch.setattr(auth_request, "_call", verify)
    calls = []
    outcomes = []
    quote_headers = {
        "x-bsv-payment-version": "1.0",
        "x-bsv-payment-satoshis-required": "5",
        "x-bsv-payment-derivation-prefix": "test-quote",
        "x-bsv-payment-transports": "header,multipart",
    }

    def request(method, url, headers, data, timeout):
        calls.append((dict(headers), data, timeout))
        if len(calls) == 1:
            status, body, signed = 402, {"payment": "required"}, True
        else:
            # A journal must exist before any paid bytes leave the client.
            assert (payment.PENDING_DIR / f"{TXID}.json").exists()
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status, body, signed = outcome
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode() if body is not None else b""
        response.headers.update({
            "x-bsv-auth-request-id": headers["x-bsv-auth-request-id"],
            "x-bsv-auth-identity-key": SERVER_KEY,
            "x-bsv-auth-your-nonce": headers["x-bsv-auth-nonce"],
            "x-bsv-auth-nonce": base64.b64encode(b"n" * 32).decode(),
        })
        # The verifier binds yourNonce to the session's client nonce.
        from lib.session import load_session
        response.headers["x-bsv-auth-your-nonce"] = load_session(URL.rsplit("/", 1)[0]).client_nonce_b64
        if status == 402:
            response.headers.update(quote_headers)
        if signed:
            rid = base64.b64decode(headers["x-bsv-auth-request-id"])
            payload = serialize_response(rid, status, filter_signable_headers(response.headers), response.content or None)
            response.headers["x-bsv-auth-signature"] = hashlib.sha256(payload).hexdigest()
        return response

    monkeypatch.setattr(requests, "request", request)
    return outcomes, actions, calls, sleeps, now, quote_headers


@pytest.mark.parametrize("multipart", [False, True])
def test_lost_response_then_signed_success(server, monkeypatch, multipart):
    outcomes, actions, calls, sleeps, _, _ = server
    if multipart:
        monkeypatch.setattr(payment, "MULTIPART_THRESHOLD", 1)
    outcomes.extend([requests.ConnectionError("lost"), (200, {"ok": True}, True)])
    result = payment.paid_request("POST", URL, body='{"input": "hello"}')
    assert result.signature_verdict == "signed"
    assert len(actions) == 1
    assert actions[0]["options"]["acceptDelayedBroadcast"] is False
    assert sleeps == [2]
    first, retry = calls[1:]
    assert first[1] == retry[1]
    assert first[0].get("x-bsv-payment") == retry[0].get("x-bsv-payment")
    assert first[0]["content-type"] == retry[0]["content-type"]
    assert first[0]["x-bsv-auth-request-id"] != retry[0]["x-bsv-auth-request-id"]
    assert list(payment.PENDING_DIR.iterdir()) == []


def test_lost_then_already_claimed(server):
    outcomes, actions, calls, sleeps, _, _ = server
    outcomes.extend([requests.Timeout("lost"), (400, {"code": "ERR_PAYMENT_REPLAYED"}, True)])
    with pytest.raises(payment.PaymentError) as exc:
        payment.paid_request("POST", URL)
    assert TXID in str(exc.value)
    assert "already claimed; the service was delivered to the first request or will be refunded" in str(exc.value)
    assert len(actions) == 1 and len(calls) == 3 and sleeps == [2]
    assert (payment.PENDING_DIR / f"{TXID}.json").exists()


@pytest.mark.parametrize("outcome", [requests.ConnectionError("lost"), requests.Timeout("lost"),
    (200, None, True), (200, {"ok": True}, False), (503, None, False)])
def test_exhaustion_and_exact_hand_retry(server, monkeypatch, capsys, outcome):
    outcomes, actions, calls, sleeps, _, _ = server
    outcomes.extend([outcome] * 4)
    with pytest.raises(payment.PaymentError) as exc:
        payment.paid_request("POST", URL, body="test payload")
    path = payment.PENDING_DIR / f"{TXID}.json"
    saved = json.loads(path.read_text())
    assert saved["payment"]["transaction"] == base64.b64encode(bytes([1, 2, 3])).decode()
    assert json.loads(saved["headers"]["x-bsv-payment"]) == saved["payment"]
    assert saved["body_sha256"] == hashlib.sha256(b"test payload").hexdigest()
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(actions) == 1 and len(calls) == 5 and sleeps == [2, 5, 10]
    command = str(exc.value).split("Retry the same payment by hand: ")[1]
    argv = shlex.split(command)
    assert argv[2:] == ["retry-payment", str(path)]
    outcomes.append((200, {"ok": True}, True))
    monkeypatch.setattr(sys, "argv", argv[1:])
    helper().main()
    assert '"status": 200' in capsys.readouterr().out
    assert len(actions) == 1
    assert calls[-1][1] == calls[1][1]
    assert calls[-1][0]["x-bsv-payment"] == calls[1][0]["x-bsv-payment"]
    assert not path.exists()


def test_quote_window_includes_elapsed_request_time(server, monkeypatch):
    outcomes, actions, calls, sleeps, now, _ = server
    # One lost send leaves too little time for even the first backoff.
    original = requests.request
    def slow(**kwargs):
        try:
            return original(**kwargs)
        finally:
            if len(calls) == 2:
                now[0] += 299
    monkeypatch.setattr(requests, "request", slow)
    outcomes.append(requests.Timeout("lost"))
    with pytest.raises(payment.PaymentError):
        payment.paid_request("POST", URL)
    assert len(calls) == 2 and len(actions) == 1 and sleeps == []
    with pytest.raises(payment.PaymentError):
        now[0] += 2
        payment.retry_payment(payment.PENDING_DIR / f"{TXID}.json")
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["quote", "parse", "wallet"])
def test_pre_payment_failure(server, monkeypatch, failure):
    _, actions, calls, sleeps, _, quote_headers = server
    if failure == "quote":
        monkeypatch.setattr(requests, "request", lambda **kw: (_ for _ in ()).throw(requests.Timeout()))
    elif failure == "parse":
        quote_headers.pop("x-bsv-payment-satoshis-required")
    else:
        monkeypatch.setattr(payment, "get_public_key", lambda **kw: (_ for _ in ()).throw(RuntimeError("unavailable")))
    with pytest.raises(Exception):
        payment.paid_request("POST", URL)
    assert actions == [] and sleeps == []
    assert not payment.PENDING_DIR.exists() or list(payment.PENDING_DIR.iterdir()) == []


def test_signed_refund_internalized_once(server, monkeypatch):
    from lib import refund
    outcomes, actions, calls, sleeps, _, _ = server
    outcomes.extend([requests.Timeout("lost"), (500, {"refund": {
        "transaction": "AQID", "derivationPrefix": "refund-prefix", "derivationSuffix": "refund-suffix",
        "senderIdentityKey": SERVER_KEY, "satoshis": 5, "txid": "34" * 32}}, True)])
    imports = []
    monkeypatch.setattr(refund, "process_refund", lambda info: imports.append(info) or {"accepted": True})
    response = payment.paid_request("POST", URL)
    assert response.status_code == 500 and len(imports) == 1
    assert not (payment.PENDING_DIR / f"{TXID}.json").exists()


def test_paid_402_never_creates_second_payment(server):
    outcomes, actions, calls, sleeps, _, _ = server
    outcomes.append((402, {"payment": "required"}, True))
    response = payment.paid_request("POST", URL)
    assert response.status_code == 402
    assert len(actions) == 1 and len(calls) == 2 and sleeps == []
    assert (payment.PENDING_DIR / f"{TXID}.json").exists()


def test_signing_delay_cannot_send_after_deadline(server, monkeypatch):
    outcomes, actions, calls, _, now, _ = server
    original = auth_request.create_signature
    def slow_sign(**kwargs):
        if calls:
            now[0] += 301
        return original(**kwargs)
    monkeypatch.setattr(auth_request, "create_signature", slow_sign)
    with pytest.raises(payment.PaymentError):
        payment.paid_request("POST", URL)
    assert len(actions) == 1 and len(calls) == 1
    assert (payment.PENDING_DIR / f"{TXID}.json").exists()


def test_recovery_requires_original_wallet(server, monkeypatch):
    from lib import metanet
    outcomes, actions, calls, _, _, _ = server
    outcomes.extend([requests.Timeout("lost")] * 4)
    with pytest.raises(payment.PaymentError):
        payment.paid_request("POST", URL)
    monkeypatch.setattr(metanet, "get_identity_key", lambda: "different-wallet")
    with pytest.raises(payment.PaymentError, match="wallet identity"):
        payment.retry_payment(payment.PENDING_DIR / f"{TXID}.json")
    assert len(actions) == 1 and len(calls) == 5


def test_server_date_bounds_quote(server):
    outcomes, actions, calls, _, _, quote_headers = server
    quote_headers["Date"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    with pytest.raises(payment.PaymentError, match="expired before payment"):
        payment.paid_request("POST", URL)
    assert actions == [] and len(calls) == 1


def test_helper_does_not_import_refund_twice(server, monkeypatch, capsys):
    from lib import refund
    outcomes, _, _, _, _, _ = server
    outcomes.append((200, {"refund": {
        "transaction": "AQID", "derivationPrefix": "refund-prefix", "derivationSuffix": "refund-suffix",
        "senderIdentityKey": SERVER_KEY, "satoshis": 5, "txid": "34" * 32}}, True))
    imports = []
    monkeypatch.setattr(refund, "process_refund", lambda info: imports.append(info) or {"accepted": True})
    monkeypatch.setattr(sys, "argv", ["helper", "pay", "POST", URL])
    helper().main()
    result = json.loads(capsys.readouterr().out)
    assert result["refund"]["processed"] is True
    assert len(imports) == 1


def test_failed_refund_keeps_journal(server, monkeypatch):
    from lib import refund
    outcomes, _, _, _, _, _ = server
    outcomes.append((200, {"refund": {
        "transaction": "AQID", "derivationPrefix": "refund-prefix", "derivationSuffix": "refund-suffix",
        "senderIdentityKey": SERVER_KEY, "satoshis": 5, "txid": "34" * 32}}, True))
    monkeypatch.setattr(refund, "process_refund", lambda info: {"accepted": False})
    with pytest.raises(payment.PaymentError, match="refund internalization failed"):
        payment.paid_request("POST", URL)
    assert (payment.PENDING_DIR / f"{TXID}.json").exists()


def test_pending_receipt_reuses_payment(server):
    outcomes, actions, calls, sleeps, _, _ = server
    outcomes.extend([(202, {"code": "RECEIPT_PENDING"}, True), (200, {"ok": True}, True)])
    assert payment.paid_request("POST", URL).status_code == 200
    assert sleeps == [5] and len(actions) == 1
    assert calls[1][0]["x-bsv-payment"] == calls[2][0]["x-bsv-payment"]


def test_wallet_action_failure_does_not_send_payment(server, monkeypatch):
    _, actions, calls, sleeps, _, _ = server
    def fail(**kwargs):
        raise RuntimeError("wallet unavailable")
    monkeypatch.setattr(payment, "create_action", fail)
    with pytest.raises(payment.PaymentError, match="Failed to create payment transaction"):
        payment.paid_request("POST", URL)
    assert len(calls) == 1 and actions == [] and sleeps == []
    assert list(payment.PENDING_DIR.iterdir()) == []
