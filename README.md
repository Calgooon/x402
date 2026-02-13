# x402

Claude Code skill for making BSV-authenticated and paid API requests using [BRC-31](https://brc.dev/31) mutual authentication and [BRC-29](https://brc.dev/29) micropayments.

## Install

Requires [Claude Code](https://claude.ai/code) and [MetaNet Client](https://getmetanet.com).

```
/plugin marketplace add calgooon/x402
/plugin install x402@calgooon-x402
```

> **Troubleshooting:** If you get a "Failed to finalize marketplace cache" error, run `/plugin`, select **Manage marketplaces → Add marketplace**, and paste `https://github.com/Calgooon/x402`. Then run `/plugin install x402@calgooon-x402`.

## Prerequisites

- **MetaNet Client** desktop app running (provides wallet at `localhost:3321`)
- **Python 3** with `requests` package (`pip install requests`)

## Usage

After installing, just describe what you want in Claude Code:

- "Discover what APIs poc-server offers"
- "Call the free endpoint on poc-server with BRC-31 auth"
- "Make a paid request to poc-server"

Claude will use the x402 skill automatically when it recognizes BSV auth or payment is needed.

## What It Does

1. **Discovers** server capabilities via `/.well-known/x402-info` (including refund support)
2. **Authenticates** using BRC-31 mutual auth (automatic handshake + session management)
3. **Pays** using BRC-29 micropayments (automatic 402 handling — creates BSV transaction, retries)
4. **Refunds** automatically — if a paid request fails, the server's BRC-29 refund is auto-internalized to your wallet

## Default Test Server

The skill includes a reference server for testing:

- **Live:** `https://poc-server.dev-a3e.workers.dev`
  - `POST /free` — Auth only (no cost)
  - `POST /paid` — Auth + 10 sat payment

## Protocol

- **BRC-31**: Mutual HTTP authentication with identity keys and signed requests
- **BRC-29**: HTTP micropayments via 402 Payment Required flow
- **BRC-100**: MetaNet Client wallet interface (key derivation, signing, transaction creation)

## License

MIT
