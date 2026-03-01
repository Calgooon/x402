# Utilities — BRC-31 Authentication & BRC-29 Payment Client Library
> Python client library implementing BRC-31 mutual authentication and BRC-29 micropayments over HTTP, backed by MetaNet Client wallet.

## Overview

This library provides the complete client-side implementation for BSV-authenticated and paid HTTP requests. It handles:

- **BRC-31 mutual authentication**: Handshake, session management, request signing, and BRC-104 header construction
- **BRC-29 micropayments**: Parsing 402 responses, deriving payment keys, building P2PKH transactions, and auto-retrying with payment
- **Binary serialization**: Wire-compatible request/response serialization matching the TS SDK and Rust server implementations
- **MetaNet Client integration**: Wallet operations (identity keys, key derivation, signatures, transaction creation) via the local HTTP API

All cryptographic operations are delegated to MetaNet Client (localhost:3321). The library uses the `requests` HTTP library for both wallet calls and server requests.

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | Package marker; no public exports |
| `auth_request.py` | Main entry point for BRC-31 authenticated requests |
| `handshake.py` | BRC-31 initialRequest/initialResponse handshake flow |
| `headers.py` | BRC-104 header filtering, auth header construction, handshake body building |
| `metanet.py` | HTTP wrapper for MetaNet Client wallet API (localhost:3321) |
| `nonce.py` | Nonce and request ID generation (32-byte random, base64 encoding) |
| `payment.py` | BRC-29 payment construction and 402 auto-retry flow |
| `serialize.py` | Binary serialization/deserialization of BRC-31 request/response payloads |
| `session.py` | Session persistence to disk (`~/.local/share/brc31-sessions/`) |

## Key Functions

### authenticated_request (auth_request.py)

```python
def authenticated_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
    session: Session | None = None,
) -> requests.Response
```
- **Purpose:** Make a fully signed BRC-31 authenticated HTTP request. This is the Python equivalent of `AuthFetch.fetch()` from the TS SDK.
- **Flow:** Parse URL → get/create session → normalize body (compact JSON) → generate nonce + request ID → filter signable headers → serialize to binary → sign via wallet → attach BRC-104 auth headers → send request
- **Returns:** Raw `requests.Response` — caller inspects status (200 = success, 402 = payment needed, 401 = session expired)
- **Raises:** `AuthRequestError` for local protocol failures; `MetaNetClientError` for wallet issues

### paid_request (payment.py)

```python
def paid_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
    max_payment_attempts: int = 3,
) -> requests.Response
```
- **Purpose:** High-level entry point for paid endpoints. Makes an authenticated request, and if the server returns 402, automatically constructs and submits a BRC-29 payment, then retries.
- **Flow:** `authenticated_request()` → if 402: parse payment headers → `create_payment()` → retry with `x-bsv-payment` header → repeat up to `max_payment_attempts`
- **Payment transport:** If payment JSON exceeds 6KB and there's no original body, sends payment in the request body (`x-bsv-payment: body`); otherwise sends payment JSON in the header value.

### do_handshake (handshake.py)

```python
def do_handshake(server_url: str) -> Session
```
- **Purpose:** Perform the BRC-31 initialRequest/initialResponse exchange with `{server_url}/.well-known/auth`.
- **Flow:** Generate 32-byte client nonce → get identity key from wallet → POST initialRequest JSON → parse server's initialResponse (body or headers) → validate `yourNonce` echo → persist and return `Session`
- **Raises:** `HandshakeError` on connection, timeout, missing fields, or nonce mismatch

### get_or_create_session (handshake.py)

```python
def get_or_create_session(server_url: str, ttl: int = 3600) -> Session
```
- **Purpose:** Load a cached session from disk or create a new one via handshake. Default TTL is 1 hour.

### serialize_request / serialize_response (serialize.py)

```python
def serialize_request(
    request_id_bytes: bytes,
    method: str,
    path: str | None,
    query: str | None,
    signable_headers: list[tuple[str, str]],
    body: bytes | None,
) -> bytes

def serialize_response(
    request_id_bytes: bytes,
    status_code: int,
    signable_headers: list[tuple[str, str]],
    body: bytes | None,
) -> bytes
```
- **Purpose:** Encode HTTP requests/responses into BRC-31 binary wire format for signing/verification.
- **Wire format (request):** `[32-byte request_id][varint method][optional path][optional query][varint header_count + pairs][optional body]`
- **Wire format (response):** `[32-byte request_id][varint status_code][varint header_count + pairs][optional body]`
- **Empty sentinel:** 9 bytes of `0xFF` encodes missing/empty optional fields (path, query, body). Matches TS SDK `writeVarIntNum(-1)`.

### deserialize_request / deserialize_response (serialize.py)

```python
def deserialize_request(payload: bytes) -> dict
def deserialize_response(payload: bytes) -> dict
```
- **Purpose:** Decode BRC-31 binary payloads back into components. Returns dicts with keys like `request_id`, `method`, `path`, `query`, `headers`, `body` (or `status_code` for responses).

### filter_signable_headers (headers.py)

```python
def filter_signable_headers(headers: dict[str, str]) -> list[tuple[str, str]]
```
- **Purpose:** Select and sort headers that are included in the BRC-31 signature.
- **Rules:** Include `x-bsv-*` (excluding `x-bsv-auth-*`), `authorization`, and `content-type` (media-type only, params stripped). Output is sorted alphabetically by lowercase key.

### build_auth_headers (headers.py)

```python
def build_auth_headers(
    identity_key: str,
    message_type: str,
    nonce_b64: str,
    your_nonce_b64: str | None = None,
    signature_hex: str = "",
    request_id_b64: str | None = None,
    initial_nonce_b64: str | None = None,
) -> dict[str, str]
```
- **Purpose:** Construct the `x-bsv-auth-*` header dict for a BRC-31 message.
- **Headers produced:** `x-bsv-auth-version`, `x-bsv-auth-identity-key`, `x-bsv-auth-message-type`, `x-bsv-auth-nonce`, and conditionally `x-bsv-auth-your-nonce`, `x-bsv-auth-signature`, `x-bsv-auth-request-id`, `x-bsv-auth-initial-nonce`.

### MetaNet Client functions (metanet.py)

| Function | Wallet API | Purpose |
|----------|-----------|---------|
| `get_identity_key()` | `POST /getPublicKey` | Get wallet's identity public key (66-char hex) |
| `get_public_key(protocol_id, key_id, counterparty, for_self)` | `POST /getPublicKey` | BRC-42 key derivation for payment/auth keys |
| `create_signature(data, protocol_id, key_id, counterparty)` | `POST /createSignature` | Sign data with derived ECDSA key |
| `create_action(outputs, description, ...)` | `POST /createAction` | Create and broadcast a BSV transaction |

All MetaNet functions communicate via JSON. Byte arrays are sent/received as JSON number arrays `[0-255]` and converted to/from Python `bytes`.

### create_payment (payment.py)

```python
def create_payment(
    derivation_prefix: str,
    server_identity_key: str,
    satoshis: int,
    server_url: str = "",
) -> dict
```
- **Purpose:** Build a complete BRC-29 payment: derive key → build P2PKH script → create transaction → return payment JSON.
- **Returns:** `{"derivationPrefix": "...", "derivationSuffix": "...", "transaction": "<base64 BEEF tx>"}`
- **Key derivation:** Uses protocol `[2, "3241645161d8"]` with key ID `"{derivationPrefix} {derivationSuffix}"` and the server's identity key as counterparty.

### Session (session.py)

```python
class Session:
    server_url: str
    server_identity_key: str
    server_nonce_b64: str
    client_nonce_b64: str
    timestamp: float
```
- **Persistence:** JSON files in `~/.local/share/brc31-sessions/`, named by first 16 hex chars of `SHA256(server_url)`.
- **TTL:** Default 1 hour. Expired sessions are auto-deleted on load.
- **Functions:** `save_session()`, `load_session()`, `clear_session()`, `clear_all_sessions()`

## Protocol Constants

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| `AUTH_PROTOCOL` | `[2, "auth message signature"]` | auth_request.py, handshake.py | BRC-31 general message signature protocol ID |
| `PAYMENT_PROTOCOL` | `[2, "3241645161d8"]` | payment.py | BRC-29 payment key derivation protocol ID |
| `AUTH_VERSION_VALUE` | `"0.1"` | headers.py | BRC-104 auth version |
| `PAYMENT_VERSION` | `"1.0"` | payment.py | BRC-29 payment protocol version |
| `HANDSHAKE_PATH` | `"/.well-known/auth"` | handshake.py | Server handshake endpoint |
| `METANET_URL` | `"http://localhost:3321"` | metanet.py | MetaNet Client wallet address |
| `EMPTY_SENTINEL` | `b'\xff' * 9` | serialize.py | Binary "missing/empty" marker in wire format |
| `HEADER_SIZE_THRESHOLD` | `6144` (6KB) | payment.py | Payment JSON size limit for header transport |

## Exception Hierarchy

| Exception | Module | Raised When |
|-----------|--------|-------------|
| `AuthRequestError` | auth_request.py | Local protocol failures (serialization, signing, wallet errors) |
| `HandshakeError` | handshake.py | Handshake connection, timeout, missing fields, nonce mismatch |
| `PaymentError` | payment.py | Payment construction/submission failures, invalid 402 headers |
| `MetaNetClientError` | metanet.py | Wallet unreachable, timeout, or error response |

## Request Flow

### Authenticated Request (no payment)
```
authenticated_request(method, url, headers, body)
  ├─ urlparse(url)
  ├─ get_or_create_session(server_base)
  │   ├─ load_session() — check disk cache
  │   └─ do_handshake() — if no valid session
  │       └─ POST /.well-known/auth (initialRequest → initialResponse)
  ├─ normalize body (compact JSON)
  ├─ generate_nonce() + generate_request_id()
  ├─ filter_signable_headers(headers)
  ├─ serialize_request(...) — BRC-31 binary format
  ├─ create_signature(serialized, ...) — via MetaNet Client
  ├─ build_auth_headers(...) — x-bsv-auth-* headers
  └─ requests.request(method, url, merged_headers, body)
```

### Paid Request (with 402 handling)
```
paid_request(method, url, headers, body)
  ├─ authenticated_request(method, url, headers, body)
  ├─ if response.status == 402:
  │   ├─ parse_402_response(response)  — extract satoshis, derivation_prefix
  │   ├─ create_payment(derivation_prefix, server_key, satoshis)
  │   │   ├─ generate random derivation_suffix
  │   │   ├─ get_public_key(PAYMENT_PROTOCOL, key_id, server_key)
  │   │   ├─ build_p2pkh_script(derived_pubkey)
  │   │   └─ create_action(outputs, ...) — via MetaNet Client
  │   ├─ attach payment as x-bsv-payment header (or body if >6KB)
  │   └─ authenticated_request(method, url, headers_with_payment, body)
  └─ return response
```

## Usage Patterns

### From the skill CLI (cli.py)
The library is called from `skills/x402/cli.py` which provides the user-facing interface. Typical usage:

```python
from lib.auth_request import authenticated_request
from lib.payment import paid_request

# Simple authenticated GET
response = authenticated_request("GET", "https://server.example/api/data")

# POST with automatic 402 payment handling
response = paid_request(
    "POST",
    "https://server.example/paid-endpoint",
    headers={"content-type": "application/json"},
    body='{"key": "value"}',
)
```

### JSON body normalization
String bodies that are valid JSON are automatically compacted to `json.dumps(json.loads(body), separators=(",", ":"))` to ensure signed bytes match what the server reconstructs. Non-JSON string bodies are encoded as UTF-8 unchanged.

### Crypto fallback
`payment.py` includes a pure-Python RIPEMD-160 implementation (`_ripemd160_pure`) as a fallback for systems where OpenSSL has RIPEMD-160 disabled (FIPS mode). The primary path uses `hashlib.new("ripemd160")`.

## Important Implementation Details

- **Signing key ID format:** `"{per_message_nonce_b64} {server_session_nonce_b64}"` — space-separated base64 strings
- **Payment key ID format:** `"{derivation_prefix} {derivation_suffix}"` — space-separated strings
- **Content-type handling:** If body is present but no content-type header is provided, defaults to `application/json`
- **Output ordering:** `create_action` is called with `randomize_outputs=False` to ensure the payment output is at index 0 (required by BRC-29)
- **Transaction format:** Transactions from `create_action` are base64-encoded BEEF format for the `x-bsv-payment` header
- **All wallet byte arrays** are transmitted as JSON number arrays `[0, 1, ..., 255]` and converted to/from Python `bytes`
