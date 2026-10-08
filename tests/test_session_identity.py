"""A BRC-31 session is never reused across two wallets (Calgooon/agents#38)."""
import json

from conftest import KEY_A, KEY_B, SERVER

from lib import metanet
from lib.auth_request import authenticated_request
from lib.handshake import do_handshake, get_or_create_session
from lib.session import (
    Session,
    _legacy_session_path,
    _session_path,
    clear_session,
    load_session,
    save_session,
)


def _session(client_key, server=SERVER):
    return Session(
        server_url=server,
        server_identity_key="02" + "5e" * 32,
        server_nonce_b64="c2VydmVyLW5vbmNl",
        client_nonce_b64="Y2xpZW50LW5vbmNl",
        client_identity_key=client_key,
    )


# --- wallet URL ------------------------------------------------------------

def test_default_wallet_url(net):
    assert metanet.wallet_url() == "http://localhost:3321"
    assert metanet.get_identity_key() == KEY_A
    assert net.wallet_calls[0][0] == "http://localhost:3321/getPublicKey"


def test_env_var_selects_wallet_url(net, use_wallet):
    use_wallet("http://localhost:3322/")
    assert metanet.wallet_url() == "http://localhost:3322"
    assert metanet.get_identity_key() == KEY_B
    metanet.create_signature(b"x", [2, "auth message signature"], "k", KEY_A)
    assert [u for u, _ in net.wallet_calls] == [
        "http://localhost:3322/getPublicKey",
        "http://localhost:3322/createSignature",
    ]


def test_identity_cached_per_process(net):
    metanet.get_identity_key()
    metanet.get_identity_key()
    assert len(net.wallet_calls) == 1


# --- session keying --------------------------------------------------------

def test_file_name_hash_changes_with_identity():
    assert _session_path(SERVER, KEY_A) != _session_path(SERVER, KEY_B)
    assert _session_path(SERVER, KEY_A) == _session_path(SERVER, KEY_A)
    assert _session_path(SERVER, KEY_A) != _legacy_session_path(SERVER)


def test_two_identities_on_one_server_get_two_sessions(net, use_wallet):
    use_wallet("http://localhost:3321")
    sa = get_or_create_session(SERVER)
    use_wallet("http://localhost:3322")
    sb = get_or_create_session(SERVER)

    assert net.handshakes == [KEY_A, KEY_B]
    assert sa.client_identity_key == KEY_A
    assert sb.client_identity_key == KEY_B
    assert sa.server_nonce_b64 != sb.server_nonce_b64
    assert _session_path(SERVER, KEY_A).exists()
    assert _session_path(SERVER, KEY_B).exists()

    # Each wallet gets its own session back without a new handshake.
    use_wallet("http://localhost:3321")
    assert get_or_create_session(SERVER).server_nonce_b64 == sa.server_nonce_b64
    assert len(net.handshakes) == 2


def test_session_of_identity_a_not_returned_for_b(net, use_wallet):
    save_session(_session(KEY_A))
    use_wallet("http://localhost:3322")  # wallet now reports B
    assert load_session(SERVER) is None
    assert load_session(SERVER, client_identity_key=KEY_B) is None


def test_mismatched_stored_identity_is_discarded(net):
    # A file at B's path whose content claims identity A (tampered/corrupt).
    path = _session_path(SERVER, KEY_B)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_session(KEY_A).to_dict()))
    assert load_session(SERVER, client_identity_key=KEY_B) is None
    assert not path.exists()


def test_legacy_file_without_identity_is_removed(net):
    legacy = _legacy_session_path(SERVER)
    legacy.parent.mkdir(parents=True)
    d = _session(KEY_A).to_dict()
    del d["client_identity_key"]
    legacy.write_text(json.dumps(d))
    assert load_session(SERVER) is None
    assert not legacy.exists()


def test_file_without_identity_field_is_stale(net):
    path = _session_path(SERVER, KEY_A)
    path.parent.mkdir(parents=True)
    d = _session(KEY_A).to_dict()
    del d["client_identity_key"]
    path.write_text(json.dumps(d))
    assert load_session(SERVER) is None
    assert not path.exists()


def test_clear_session_only_clears_current_identity(net, use_wallet):
    save_session(_session(KEY_A))
    save_session(_session(KEY_B))
    use_wallet("http://localhost:3322")
    assert clear_session(SERVER) is True
    assert not _session_path(SERVER, KEY_B).exists()
    assert _session_path(SERVER, KEY_A).exists()


# --- authenticated requests ------------------------------------------------

def test_wallet_switch_signs_with_own_session(net, use_wallet):
    use_wallet("http://localhost:3321")
    authenticated_request("GET", SERVER + "/free")
    use_wallet("http://localhost:3322")
    authenticated_request("GET", SERVER + "/free")
    assert net.handshakes == [KEY_A, KEY_B]
    assert [k for _, k in net.general] == [KEY_A, KEY_B]


def test_supplied_session_of_other_identity_is_ignored(net, use_wallet):
    use_wallet("http://localhost:3322")
    authenticated_request("GET", SERVER + "/free", session=_session(KEY_A))
    assert net.handshakes == [KEY_B]


def test_401_recovers_once_without_looping(net):
    do_handshake(SERVER)
    net.general_status = 401
    resp = authenticated_request("GET", SERVER + "/free")
    assert resp.status_code == 401
    # original request + exactly one retry, after exactly one re-handshake
    assert len(net.general) == 2
    assert net.handshakes == [KEY_A, KEY_A]
    assert _session_path(SERVER, KEY_A).exists()


def test_401_then_success(net, monkeypatch):
    import requests

    do_handshake(SERVER)
    statuses = iter([401, 200])
    orig = net.request

    def flaky(*a, **kw):
        net.general_status = next(statuses)
        return orig(*a, **kw)

    monkeypatch.setattr(requests, "request", flaky)
    resp = authenticated_request("GET", SERVER + "/free")
    assert resp.status_code == 200
    assert len(net.general) == 2
    assert len(net.handshakes) == 2
