"""BRC-31 session persistence.

Stores BRC-31 sessions per (server, client identity) so we can reuse them
without re-handshaking. Sessions are saved as JSON files in
~/.local/share/brc31-sessions/.

Design decisions:
- XDG data dir pattern (~/.local/share/)
- File named by first 16 hex chars of
  SHA256(server_url + "\n" + client_identity_key). A session belongs to the
  wallet that opened it: a second wallet talking to the same server gets its
  own file and its own handshake, never the first wallet's session.
- The client identity key is also stored in the file; ``load_session``
  discards a session whose stored identity differs from the wallet's current
  one. Files written before this field existed are treated as stale.
- JSON format for easy debugging/inspection
- Default 1 hour TTL; expired sessions auto-cleaned on load
"""

import json
import hashlib
import time
from pathlib import Path


SESSION_DIR = Path.home() / ".local" / "share" / "brc31-sessions"
DEFAULT_TTL = 3600  # 1 hour

# Non-session files that share SESSION_DIR (see lib/registry.py).
_NON_SESSION_FILES = {"registry.json"}


class Session:
    """A BRC-31 session with a specific server."""

    def __init__(
        self,
        server_url: str,
        server_identity_key: str,
        server_nonce_b64: str,
        client_nonce_b64: str,
        client_identity_key: str,
        timestamp: float | None = None,
    ):
        self.server_url = server_url
        self.client_identity_key = client_identity_key
        self.server_identity_key = server_identity_key
        self.server_nonce_b64 = server_nonce_b64
        self.client_nonce_b64 = client_nonce_b64
        self.timestamp = timestamp or time.time()

    def is_expired(self, ttl: int = DEFAULT_TTL) -> bool:
        """Check whether this session has exceeded its TTL."""
        return time.time() - self.timestamp > ttl

    def to_dict(self) -> dict:
        """Serialize session to a plain dict."""
        return {
            "server_url": self.server_url,
            "server_identity_key": self.server_identity_key,
            "server_nonce_b64": self.server_nonce_b64,
            "client_nonce_b64": self.client_nonce_b64,
            "client_identity_key": self.client_identity_key,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        """Deserialize a session from a plain dict."""
        return cls(
            server_url=d["server_url"],
            server_identity_key=d["server_identity_key"],
            server_nonce_b64=d["server_nonce_b64"],
            client_nonce_b64=d["client_nonce_b64"],
            client_identity_key=d["client_identity_key"],
            timestamp=d.get("timestamp", 0),
        )

    def __repr__(self) -> str:
        age = time.time() - self.timestamp
        return (
            f"Session(server_url={self.server_url!r}, "
            f"server_identity_key={self.server_identity_key[:16]}..., "
            f"client_identity_key={self.client_identity_key[:16]}..., "
            f"age={age:.0f}s)"
        )


def _current_identity() -> str:
    """The identity key of the wallet this process talks to (cached)."""
    from lib.metanet import get_identity_key
    return get_identity_key()


def _legacy_session_path(server_url: str) -> Path:
    """Pre-0.2 file name: hash of the server URL only (no identity)."""
    url_hash = hashlib.sha256(server_url.encode()).hexdigest()[:16]
    return SESSION_DIR / f"{url_hash}.json"


def _session_path(server_url: str, client_identity_key: str) -> Path:
    """Get the file path for a session, keyed by (server URL, client identity)."""
    key = f"{server_url}\n{client_identity_key}"
    key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
    return SESSION_DIR / f"{key_hash}.json"


def save_session(session: Session) -> Path:
    """Save a session to disk. Returns the file path."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = _session_path(session.server_url, session.client_identity_key)
    path.write_text(json.dumps(session.to_dict(), indent=2))
    return path


def load_session(
    server_url: str,
    ttl: int = DEFAULT_TTL,
    client_identity_key: str | None = None,
) -> Session | None:
    """Load the session the current wallet holds with ``server_url``.

    ``client_identity_key`` defaults to the wallet's identity key (one
    cached ``getPublicKey`` call per process). Returns None if there is no
    session, it expired, or it was opened by a different identity; stale
    files are deleted.
    """
    if client_identity_key is None:
        client_identity_key = _current_identity()

    # A pre-identity session file for this server cannot be attributed to
    # any wallet: drop it.
    _legacy_session_path(server_url).unlink(missing_ok=True)

    path = _session_path(server_url, client_identity_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        session = Session.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        path.unlink(missing_ok=True)
        return None
    if (
        session.client_identity_key != client_identity_key
        or session.server_url != server_url
        or session.is_expired(ttl)
    ):
        path.unlink(missing_ok=True)
        return None
    return session


def clear_session(server_url: str, client_identity_key: str | None = None) -> bool:
    """Delete the current wallet's stored session with ``server_url``.

    Also removes any pre-identity (URL-only) file for that server.
    Returns True if a file was deleted.
    """
    if client_identity_key is None:
        client_identity_key = _current_identity()
    deleted = False
    for path in (
        _session_path(server_url, client_identity_key),
        _legacy_session_path(server_url),
    ):
        if path.exists():
            path.unlink()
            deleted = True
    return deleted


def list_session_files() -> list[Path]:
    """List session filenames without exposing identities or session nonces."""
    return sorted(f for f in SESSION_DIR.glob("*.json")
                  if f.name not in _NON_SESSION_FILES)


def clear_all_sessions() -> int:
    """Delete all stored sessions (every identity). Returns count deleted."""
    if not SESSION_DIR.exists():
        return 0
    count = 0
    for f in list_session_files():
        f.unlink()
        count += 1
    return count


if __name__ == "__main__":
    from lib.nonce import generate_nonce_base64

    url = "https://poc-server.dev-a3e.workers.dev"
    identity_key = "02ce71d2f3a5a863ab5f2f12e52fdbaea2fba5ae9a212ac585dbb71608e218e40b"
    client_key = "03" + "11" * 32

    # Create a session
    session = Session(
        server_url=url,
        server_identity_key=identity_key,
        server_nonce_b64=generate_nonce_base64(),
        client_nonce_b64=generate_nonce_base64(),
        client_identity_key=client_key,
    )
    print(f"Created:  {session}")
    print(f"Expired:  {session.is_expired()}")

    # Save it
    path = save_session(session)
    print(f"Saved to: {path}")

    # Load it back
    loaded = load_session(url, client_identity_key=client_key)
    assert loaded is not None
    print(f"Loaded:   {loaded}")
    print(f"Match:    {loaded.server_nonce_b64 == session.server_nonce_b64}")

    # Clear it
    deleted = clear_session(url, client_identity_key=client_key)
    print(f"Cleared:  {deleted}")

    # Verify it is gone
    gone = load_session(url, client_identity_key=client_key)
    print(f"Gone:     {gone is None}")
