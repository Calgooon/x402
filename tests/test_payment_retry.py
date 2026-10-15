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
    code = "ERR_PAYMENT_REPLAYED"
    outcomes, actions, calls, sleeps, _, _ = server
    outcomes.extend([requests.Timeout("lost"), (400, {"code": code}, True)])
    with pytest.raises(payment.PaymentError) as exc:
        payment.paid_request("POST", URL)
    assert TXID in str(exc.value)
    assert "already claimed; the service was delivered to the first request or will be refunded" in str(exc.value)
    assert len(actions) == 1 and len(calls) == 3 and sleeps == [2]
    assert not (payment.PENDING_DIR / f"{TXID}.json").exists()
    assert "retry-payment" not in str(exc.value)


def test_lost_then_quote_expired_unclaimed(server, caplog):
    outcomes, actions, calls, sleeps, now, _ = server
    outcomes.extend([requests.Timeout("lost"),
                     (400, {"code": "ERR_QUOTE_EXPIRED", "message": "quote gone"}, True)])
    with pytest.raises(payment.PaymentError) as exc:
        payment.paid_request("POST", URL)
    path = payment.PENDING_DIR / f"{TXID}.json"
    saved = json.loads(path.read_text())
    assert saved["state"] == "expired-unclaimed"
    assert saved["terminal_at"] == now[0]
    assert saved["server_code"] == "ERR_QUOTE_EXPIRED"
    assert saved["server_message"] == "quote gone"
    for words in ("the quote expired", "the server may not have claimed this payment",
                  "the sats are yours if no service was delivered", "keep this file",
                  "refund path or a later receipt query", "import-refund"):
        assert words in str(exc.value) and words in caplog.text
    assert "retry-payment" not in str(exc.value)
    assert len(actions) == 1 and len(calls) == 3 and sleeps == [2]
    with pytest.raises(payment.PaymentError, match="keep this file"):
        payment.retry_payment(path)
    assert len(calls) == 3


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


@pytest.mark.parametrize("signed", [True, False])
def test_paid_402_never_creates_second_payment(server, signed):
    outcomes, actions, calls, sleeps, _, _ = server
    outcomes.append((402, {"payment": "required"}, signed))
    with pytest.raises(payment.PaymentError, match=TXID):
        payment.paid_request("POST", URL)
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


@pytest.mark.parametrize("offset, expected", [(-1000, 1300), (-61, 1300), (-60, 1240), (-30, 1270), (0, 1300), (60, 1300)])
def test_server_date_bounds_quote(server, offset, expected):
    from email.utils import formatdate
    outcomes, actions, calls, _, _, quote_headers = server
    quote_headers["Date"] = formatdate(1000 + offset, usegmt=True)
    outcomes.extend([requests.Timeout("lost")] * 4)
    with pytest.raises(payment.PaymentError):
        payment.paid_request("POST", URL)
    record = json.loads((payment.PENDING_DIR / f"{TXID}.json").read_text())
    assert record["expires_at"] == expected
    assert len(actions) == 1


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


@pytest.mark.parametrize("field", ["refund", "excessRefund"])
@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_failed_refund_preserves_response_and_manual_import(server, monkeypatch, capsys, field, failure):
    from lib import refund
    outcomes, actions, calls, _, _, _ = server
    body = {"answer": "delivered result", field: {
        "transaction": "AQID", "derivationPrefix": "refund-prefix", "derivationSuffix": "refund-suffix",
        "senderIdentityKey": SERVER_KEY, "satoshis": 5, "txid": "34" * 32}}
    outcomes.append((200, body, True))
    def fail(info):
        if failure == "exception":
            raise RuntimeError("wallet unavailable")
        return {"accepted": False}
    monkeypatch.setattr(refund, "process_refund", fail)
    response = payment.paid_request("POST", URL)
    assert response.json() == body
    assert response.refund_result["processed"] is False
    path = payment.PENDING_DIR / f"{TXID}.json"
    saved = json.loads(path.read_text())
    assert base64.b64decode(saved["response"]["body"]) == response.content
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(payment.PaymentError, match="response already saved"):
        payment.retry_payment(path)
    imports = []
    monkeypatch.setattr(refund, "process_refund", lambda info: imports.append(info) or {"accepted": True})
    argv = shlex.split(response.refund_result["command"])
    assert argv[2:] == ["import-refund", str(path)]
    monkeypatch.setattr(sys, "argv", argv[1:])
    helper().main()
    assert json.loads(capsys.readouterr().out)["accepted"] is True
    assert payment.import_refund(path)["already_imported"] is True
    assert len(imports) == 1 and imports[0].transaction == body[field]["transaction"]
    assert json.loads(path.read_text())["refund_imported"] is True
    assert len(actions) == 1 and len(calls) == 2


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


@pytest.mark.parametrize("hand_retry", [False, True])
def test_first_send_or_hand_retry_quote_expired_is_terminal(server, hand_retry):
    outcomes, actions, calls, _, _, _ = server
    path = payment.PENDING_DIR / f"{TXID}.json"
    if hand_retry:
        outcomes.extend([requests.Timeout("lost")] * 4)
        with pytest.raises(payment.PaymentError):
            payment.paid_request("POST", URL)
    outcomes.append((400, {"code": "ERR_QUOTE_EXPIRED", "message": "fresh quote"}, True))
    with pytest.raises(payment.PaymentError, match=TXID) as exc:
        if hand_retry:
            payment.retry_payment(path)
        else:
            payment.paid_request("POST", URL)
    assert "the server may not have claimed this payment" in str(exc.value)
    assert "retry-payment" not in str(exc.value)
    assert len(actions) == 1 and path.exists()
    saved = json.loads(path.read_text())
    assert saved["state"] == "expired-unclaimed"
    assert saved["server_code"] == "ERR_QUOTE_EXPIRED"
    assert saved["server_message"] == "fresh quote"


def test_error_message_text_does_not_establish_replay(server):
    outcomes, actions, _, _, _, _ = server
    outcomes.append((400, {"code": "OTHER", "message": "already claimed payment replayed ERR_QUOTE_EXPIRED"}, True))
    assert payment.paid_request("POST", URL).status_code == 400
    assert len(actions) == 1
    assert (payment.PENDING_DIR / f"{TXID}.json").exists()


def test_slow_paid_response_keeps_full_read_timeout(server, monkeypatch):
    outcomes, actions, calls, _, now, _ = server
    original_create = payment.create_payment
    def slow_create(*args):
        result = original_create(*args)
        now[0] += 250
        return result
    monkeypatch.setattr(payment, "create_payment", slow_create)
    original_request = requests.request
    def slow_response(**kwargs):
        response = original_request(**kwargs)
        if len(calls) == 2:
            assert kwargs["timeout"] == 300
            now[0] += 240
        return response
    monkeypatch.setattr(requests, "request", slow_response)
    outcomes.append((200, {"answer": "slow result"}, True))
    assert payment.paid_request("POST", URL).json()["answer"] == "slow result"
    assert len(actions) == 1 and len(calls) == 2
    assert now[0] > 1300


@pytest.mark.parametrize("expired", [False, True])
def test_paid_session_refresh_preserves_deadline(server, monkeypatch, expired):
    outcomes, actions, calls, _, now, _ = server
    outcomes.extend([(401, {"code": "SESSION_EXPIRED"}, True), (200, {"ok": True}, True)])
    original = auth_request.clear_session
    def refresh(*args, **kwargs):
        original(*args, **kwargs)
        if expired:
            now[0] += 301
    monkeypatch.setattr(auth_request, "clear_session", refresh)
    if expired:
        with pytest.raises(payment.PaymentError) as exc:
            payment.paid_request("POST", URL)
        assert "retry-payment" not in str(exc.value)
        assert "the server may not have claimed this payment" in str(exc.value)
        assert len(calls) == 2
    else:
        assert payment.paid_request("POST", URL).status_code == 200
        assert calls[1][1] == calls[2][1]
        assert calls[1][0]["x-bsv-payment"] == calls[2][0]["x-bsv-payment"]
    assert len(actions) == 1


@pytest.mark.parametrize("multipart", [False, True])
def test_quoted_and_paid_body_bytes_match(server, monkeypatch, multipart):
    outcomes, _, calls, _, _, _ = server
    if multipart:
        monkeypatch.setattr(payment, "MULTIPART_THRESHOLD", 1)
    outcomes.append((200, {"ok": True}, True))
    payment.paid_request("POST", URL, body=' { "input": "hello" } ')
    assert calls[0][1] == b'{"input":"hello"}'
    assert calls[0][0]["content-type"] == "application/json"
    if multipart:
        assert b"Content-Type: application/json\r\n\r\n" + calls[0][1] + b"\r\n" in calls[1][1]
    else:
        assert calls[0][1] == calls[1][1]
        assert calls[0][0]["content-type"] == calls[1][0]["content-type"]


def test_pending_write_failure_prints_payment_json(server, monkeypatch, capsys):
    _, actions, calls, _, _, _ = server
    def fail(record):
        raise OSError("disk full")
    monkeypatch.setattr(payment, "_save_pending", fail)
    with pytest.raises(payment.PaymentError, match=TXID):
        payment.paid_request("POST", URL)
    payment_json = capsys.readouterr().err.split("recovery JSON: ")[1]
    assert json.loads(payment_json)["transaction"] == "AQID"
    assert len(actions) == 1 and len(calls) == 1


def test_refund_save_failure_returns_response_and_prints_recovery(server, monkeypatch, capsys):
    from lib import refund
    outcomes, actions, calls, _, _, _ = server
    body = {"answer": "delivered", "excessRefund": {
        "transaction": "AQID", "derivationPrefix": "refund-prefix", "derivationSuffix": "refund-suffix",
        "senderIdentityKey": SERVER_KEY, "satoshis": 5, "txid": "34" * 32}}
    outcomes.append((200, body, True))
    monkeypatch.setattr(refund, "process_refund", lambda info: {"accepted": False})
    save = payment._save_pending
    def fail_response_save(record, path=None):
        if "response" in record:
            raise OSError("disk full")
        return save(record, path)
    monkeypatch.setattr(payment, "_save_pending", fail_response_save)
    response = payment.paid_request("POST", URL)
    assert response.json() == body and response.refund_result["processed"] is False
    saved = json.loads(capsys.readouterr().err.split("Recovery JSON: ")[1])
    assert json.loads(base64.b64decode(saved["response"]["body"])) == body
    assert len(actions) == 1 and len(calls) == 2


@pytest.mark.parametrize("status, code", [(201, None), (400, "ERR_INVALID_PAYMENT"),
                                         (500, "ERR_PAYMENT_FAILED")])
def test_unconfirmed_response_keeps_terminal_record(server, status, code):
    outcomes, actions, calls, _, now, _ = server
    outcomes.append((status, {"code": code, "message": "unconfirmed"}, True))
    assert payment.paid_request("POST", URL).status_code == status
    path = payment.PENDING_DIR / f"{TXID}.json"
    saved = json.loads(path.read_text())
    assert saved["state"] == "terminal-unresolved"
    assert saved["terminal_at"] == now[0]
    assert saved["server_code"] == code
    assert saved["server_message"] == "unconfirmed"
    with pytest.raises(payment.PaymentError, match="keep this file"):
        payment.retry_payment(path)
    assert len(actions) == 1 and len(calls) == 2
