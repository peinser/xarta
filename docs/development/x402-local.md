# Local x402 Development

Last verified: 2026-08-31 against official Python SDK 2.21.0.

Install the locked environment with `uv sync --frozen --all-groups`. Xarta uses
`x402[evm]`; do not manually construct EIP-3009 signatures or EIP-712 payloads.

## Configuration

Configure canonical HTTPS resource URLs, deterministic USD pricing, a reviewed markup,
and a short quote TTL. Set `X402_CHALLENGE_SIGNING_KEY` to an application secret of at
least 32 bytes. All replicas serving a challenge must be able to verify it until expiry.
This secret is not a wallet key. The current single-key configuration requires a
coordinated rotation after outstanding challenges using the old key have expired.

For Base Sepolia use:

- `X402_NETWORK=eip155:84532`;
- native USDC `0x036CbD53842c5426634e7929541eC2318f3dCF7e` with 6 decimals;
- `X402_FACILITATOR_URL=https://x402.org/facilitator` where currently supported;
- `X402_PAY_TO` as the seller's public address.

The public x402.org facilitator is rejected for Base mainnet. Production requires a
reviewed mainnet facilitator and payee configuration.

## Wallet Separation

The buyer owns the test private key and Base Sepolia USDC. The seller is only the public
`X402_PAY_TO` recipient. Xarta does not need either wallet's private key. `make standalone`
and all scenario process helpers remove `EVM_PRIVATE_KEY` and
`X402_SELLER_PRIVATE_KEY` from the server environment.

`make wallets` creates ignored test-only values in `.env`. Never commit or log that file,
reuse the wallets on mainnet, or automatically request faucet funds.

## Deterministic Scenarios

Run:

```sh
make scenario-x402-facilitator
make scenario-x402-paid-intake
```

The facilitator scenario is local HTTPS, deterministic, network-free, and
non-blockchain. It validates official SDK wire schemas, requirement matching,
verify/settle choreography, protocol payment identifiers, and synthetic transaction
hashes. Duplicate-settlement behavior belongs to that protocol double; Xarta does not
persist a separate replay ledger.

The paid-intake scenario starts Xarta and performs 50 priced multipart archive flows. It
validates HTTP 402 challenges, signed term binding, paid retries, returned synthetic
transaction hashes, real structured application log events, archive output, direct
JetStream publication, absence of all payment tables, and absence of generic PostgreSQL
tracking for the synchronous archive node.

## Base Sepolia Contract Scenario

The live scenario is excluded from `pytest`, CI, `make verify`, and `scenario-all`. It
requires a funded buyer, seller address, and explicitly configured Base Sepolia RPC:

```sh
X402_BASE_SEPOLIA_CONTRACT_TEST=true \
make scenario-x402-base-sepolia
```

Required environment variables are `EVM_PRIVATE_KEY`, `X402_PAY_TO`, and
`BASE_SEPOLIA_RPC_URL`. `X402_FACILITATOR_URL` defaults to the public test facilitator.
The scenario uses the locked official x402 client, performs one real paid archive flow,
polls the RPC transaction receipt, verifies the exact USDC contract and seller balance
increase, and proves that the returned, logged, and on-chain transaction hashes match.

The non-secret report is written to
`.dev/runtime/scenarios/x402-base-sepolia/results.json`. It never contains private keys,
seed phrases, or the RPC URL.

The live scenario proves one public-facilitator Base Sepolia settlement. It does not test
Base mainnet, a production facilitator, refunds, or the synchronous post-settlement crash
boundary documented in `docs/x402.md`.
