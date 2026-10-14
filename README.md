# x402

Claude Code skill for BSV micropayments. Discover, authenticate, and pay AI services with Bitcoin SV - all from natural language.

## What It Does

Say what you want. The skill handles the rest:

```
/x402 generate an image of a mountain sunset
/x402 transcribe this audio file
/x402 search twitter for AI agent discussions
/x402 upload this file to bsv-storage for a year
```

Under the hood:

1. **Discovers** available services from the [x402agency.com](https://x402agency.com) agent directory
2. **Authenticates** with [BRC-31](https://brc.dev/31) mutual auth (automatic handshake + session caching)
3. **Pays** with [BRC-29](https://brc.dev/29) micropayments (automatic 402 handling, transaction creation)
4. **Refunds** automatically - both service-failure refunds (a paid request fails) and excess refunds (you overpaid a token-priced agent) are detected and internalized to your wallet with no manual steps

## Available Services

The skill discovers agents from [x402agency.com/.well-known/agents](https://x402agency.com/.well-known/agents):

<!-- services:start -->
| Agent | What it does | Price range | Delivery |
|:------|:-------------|:------------|:---------|
| Nano Banana 2.1 | New - launched 2026-10-06 · AI Image Generation with Search Grounding | $0.042-$0.14175 USD (per-resolution; 1K to 4K) | async |
| Nano Banana 2 | AI Image Generation with Search Grounding | $0.08375-$0.18875 USD (per-resolution; 1K to 4K) | async |
| Nano Banana Pro | AI Image Generation with Safety Controls | $0.1875-$0.375 USD (per-resolution; 1K to 4K) | async |
| Veo Agent | AI Video Generation | $0.5-$1.5 USD (per-second; 4s silent to 8s + audio) | async |
| Whisper Agent | Speech-to-Text | $0.0006375-$0.019125 USD (per-minute; 1 min to 30 min) | sync |
| X Research Agent | Twitter/X Research API | $0.00625-$3.125 USD (per-resource; Counts: 1 page, mode recent to Search: limit 100, 5 pages (maximum)) | sync |
| Kling Agent | AI Video Generation (Kling) | $0.175-$1.155 USD (per-second; v1 Standard 5s to v3 Standard 3s + audio + voice) | async |
| 1Sat Agent | 1Sat Ordinals Inscriptions | $0.0001-$0.032 USD (per-byte (mining fee + 25%); 1 KB (200 sats min) to 500 KB max (64,000 sats)) | sync |
| OpenAI Agent | AI Chat Completions (GPT-5.x) | $0.002079-$0.94035 USD (per-token; gpt-5-nano (4K out) to gpt-5.5-pro (4K out)) | sync |
| Claude Agent | AI Chat Completions (Claude) | $0.02685-$0.2685 USD (per-token; claude-haiku-4-5 (4K out) to claude-fable-5 (4K out)) | sync |
| PolyMirror Agent | Polymarket Whale Tracking | $0.005-$0.01 USD (per-request; /leaderboard to /trader) | sync |
| BSV Storage | File Hosting (UHRP) | $0.0006083333333333333-$0.6083333333333334 USD (per-GB-month; 1 MB / 1 year (1,000-sat minimum) to 1 GB / 1 year (1,000-sat minimum)) | sync |
| MessageBox | P2P Message Relay | $0 USD (recipient-controlled; Free delivery) | sync |
| Web Reader Agent | URL → Clean Markdown | $0.003-$0.005 USD (per-request; Read URL to Web search) | sync |
<!-- services:end -->

Ranges use the minimum and maximum USD tier examples exactly as listed by the registry, with their labels and pricing model. They are not universal per-request quotes; use `discover` for endpoint pricing.

Our `bsv-storage` server uses Cloudflare R2 for bytes and on-chain UHRP advertisements; the registry hosts its discovery manifest, and the helper derives downloads on `files.x402agency.com` automatically.

## Install

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and either wallet path below.

```
/plugin marketplace add calgooon/x402
/plugin install x402@calgooon-x402
```

> **Troubleshooting:** If you get a "Failed to finalize marketplace cache" error, run `/plugin`, select **Manage marketplaces > Add marketplace**, and paste `https://github.com/Calgooon/x402`. Then run `/plugin install x402@calgooon-x402`.

### Prerequisites

Install **Python 3** with `requests` (`pip install requests`), then choose a wallet. Both paths speak BRC-100 over HTTP; the helper sends every wallet call to `X402_WALLET_URL`.

1. **For a person: MetaNet Client.** Download [MetaNet Client](https://getmetanet.com) and run it at `localhost:3321`, the helper's default wallet URL.
2. **For an agent with no human: bsv-wallet-cli.** Use the self-hosted headless daemon from the [public wallet README](https://github.com/Calhooon/bsv-wallet-cli#readme). Its current installation and inspection commands are:

   ```sh
   cargo install bsv-wallet-cli --version 0.7.4 --locked --root ./wallet-tools
   ./wallet-tools/bin/bsv-wallet --help
   ./wallet-tools/bin/bsv-wallet respend --help
   ```

   The wallet README names `daemon` for the monitor plus HTTP server and `serve` for the HTTP server alone. It directs readers to command help for arguments and to source for configuration defaults. It does not provide complete initialization/start commands, an identity command, funding commands, or the HTTP port. Follow that README and the installed command help to initialize, start, identify, and fund the wallet; those missing commands are not assumed here.

   Once it is running, set the helper's URL to its configured loopback HTTP port, replacing `<its port>`:

   ```sh
   export X402_WALLET_URL="http://127.0.0.1:<its port>"
   ```

   Check the identity through the x402 helper:

   ```sh
   python3 skills/x402/scripts/brc31_helpers.py identity
   ```

## Usage

The `/x402` command is the entry point. You can use it conversationally or with specific commands:

### Natural language
```
/x402 generate a photo of a cat in a top hat
/x402 upload report.pdf to bsv-storage for 1 year
/x402 search twitter for "bitcoin scaling" from the last 24 hours
/x402 transcribe meeting.wav
```

### Specific commands
```
/x402 list                              # List all agents in the directory
/x402 discover banana                   # Show endpoints, pricing, schemas for an agent
/x402 discover bsv-storage                # Registry-hosted storage discovery
/x402 auth GET bsv-storage/list           # Authenticated request (no payment)
/x402 pay POST banana/generate {"prompt":"a sunset"}  # Paid request
```

### Command reference

| Command | What it does |
|:--------|:-------------|
| `/x402 list` | List all registered agents with capabilities and pricing |
| `/x402 discover <agent>` | Fetch the x402-info manifest - endpoints, schemas, pricing, error codes |
| `/x402 auth <METHOD> <agent/path>` | Make a BRC-31 authenticated request (no payment) |
| `/x402 pay <METHOD> <agent/path> [body]` | Make an authenticated + paid request (auto-handles 402 flow, auto-internalizes refunds) |
| `/x402 execute-action <json>` | Broadcast a pending action template (e.g. a 1Sat inscription) returned by `pay`, after you confirm |
| `/x402 identity` | Show your wallet's identity key |
| `/x402 session <url>` | Inspect a cached BRC-31 session |

Agent names (`banana`, `bsv-storage`, `whisper`, etc.) resolve automatically. Full URLs work too.

## Examples

### Generate an image
```
/x402 generate a photo of a cat wearing a top hat
→ discovers banana agent, pays ~9,000 sats, returns image URL
```

### Upload a file

The upload command defaults to `bsv-storage` at `storage.x402agency.com`.
Storage costs $0.05 per GB-month with a 1,000-sat minimum per upload.

```
/x402 upload report.pdf to bsv-storage for 1 year
→ discovers bsv-storage, requests the exact quote (1,000-sat minimum), pays, PUTs the bytes, and verifies the public URL
```

### Search Twitter
```
/x402 find tweets about bitcoin scaling from the last 24 hours
→ discovers x-research agent, pays ~3,000 sats, returns sorted results
```

### Transcribe audio
```
/x402 transcribe meeting.wav
→ discovers whisper agent, pays based on audio length, returns text
```

### Inscribe an image as a 1Sat Ordinal
```
/x402 inscribe this image as an ordinal
→ discovers 1sat-agent, pays service fee, builds script, wallet broadcasts, returns txid
```

## How It Works

```
/x402 <user request>
  → skill runs `list` to find matching agent by capabilities
  → skill runs `discover <agent>` to read the x402-info manifest
  → skill reads endpoint schemas, pricing, auth requirements
  → skill runs `pay POST <agent>/<endpoint> '{...}'`
    → BRC-31 handshake (automatic, cached 1 hour)
    → initial request → server returns 402 with price
    → wallet creates payment transaction
    → retry with x-bsv-payment header → server accepts, returns result
  → skill presents result to user
```

All payment is automatic - no confirmation prompts for typical micropayments (1–100,000 sats).

## Wallet selection and sessions

Every wallet call goes to `X402_WALLET_URL` (default `http://localhost:3321`). Set it to use a second wallet daemon, e.g. `X402_WALLET_URL=http://localhost:3322`.

BRC-31 sessions are cached in `~/.local/share/brc31-sessions/` as `SHA256(server_url + "\n" + client_identity_key)[:16].json`, and each file records the identity that opened it. A session is only reused by the wallet that opened it: when the wallet reports a different identity key, the helper handshakes afresh. Session files from versions before 1.1.1 (keyed by server URL only) are deleted on first use.

Authenticated responses include a verdict column beside the HTTP status: `signed`, `UNSIGNED`, or `BAD SIGNATURE`. Both `auth` and `pay` verify BRC-104 response signatures with the session's server identity, request ID, status, signed headers, and raw body, including 400/402/500 refusals. Missing or invalid signatures print a warning and make the command exit nonzero. An unverified 402 never triggers payment.

Both session entries report a clear error when no URL is supplied. To preview all cached session filenames without deleting anything or prompting:

```sh
python3 skills/x402/scripts/brc31_helpers.py session --clear-all --dry-run
```

Then `session --clear-all` lists the filenames and deletes all wallets' sessions without confirmation. The registry cache is preserved. These flags also work with `skills/x402/cli.py`.

## Development

Run checks with `python3 -m pytest -q`. Regenerate the Available Services table from the live registry with `python3 scripts/gen_services.py`; do not edit the generated table by hand.

## Changelog

- **1.1.2** - Document the MetaNet Client and headless wallet paths, including gaps in the wallet README. Generate all service rows, exact registry tier ranges, and sync/async delivery from the public registry.

- **1.1.1** - Sessions keyed by (server URL, client identity key); a session opened by one wallet is never reused by another (a wallet switch on the same server previously made the server derive the BRC-29 payee key from the wrong identity). Wallet URL configurable via `X402_WALLET_URL`. `createAction` accepts `tx` returned as an index-keyed object. Added `tests/` (`python3 -m pytest -q`).

## Agent Directory

The skill resolves short names (like `banana` or `bsv-storage`) to full URLs via the [x402agency.com](https://x402agency.com) registry. Any BRC-31/BRC-29 service can be listed, including third-party services that don't serve their own discovery manifest.

The registry is cached locally for 5 minutes. Full URLs (`https://...`) bypass the registry entirely.

## Protocol

- **[BRC-31](https://brc.dev/31)**: Mutual HTTP authentication with identity keys and signed requests
- **[BRC-29](https://brc.dev/29)**: HTTP micropayments via 402 Payment Required flow
- **[BRC-100](https://brc.dev/100)**: MetaNet Client wallet interface (key derivation, signing, transaction creation)

## License

MIT
