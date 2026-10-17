# Independent collateral evidence — preparation, not financial readiness

`live-chain.cjs` reads Polygon JSON-RPC only. It has no private-key input, signer,
transaction broadcaster, token approval, ledger mutation or exchange order path.
It is a standalone draft component; financial ledger/transport integration is
still required. Neither an account balance nor a successful chain probe proves
that the bot can trade, exit, redeem or make a profit.

The pUSD address is pinned from Polymarket's current contracts reference:
`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, chain 137, six decimals.
CTF and Protocol V2 position systems are separate; this component verifies
collateral and receipt data, not ERC-1155 position ownership or OrderFilled
attribution to an approved order.

Every evidence category requires agreement between two allowlisted services:

| Evidence | Services |
| --- | --- |
| Chain identity, canonical headers, code, balances and decimals | Tenderly, dRPC |
| Transfer logs and transaction receipts | Tenderly, PublicNode |

The split follows verified public endpoint capabilities. PublicNode refused an
old balance call with HTTP 403; dRPC refused old logs with HTTP 400. These are
never interpreted as an empty account/history and never trigger a one-provider
fallback. No paid account, key or subscription is created. Public services may
still impose quotas or become unavailable; evidence then remains unavailable.
Two RPC services reduce reliance on one data source but are not a cryptographic
state proof and do not establish independent ownership of their upstream nodes.

The reader selects a common finalized block, verifies its canonical hash and
collateral balance, and rechecks canonicality. Anchors are bound to the funder
fingerprint and their balances are re-read; callers cannot substitute an
unverified starting balance. Transfer scans cover every block after the anchor
in consecutive 4,096-block chunks, with a 65,536-block capacity ceiling. Responses,
log counts and receipt sizes are bounded. Amounts and deltas remain integer base
units throughout. Duplicate, removed, unrelated and mismatched logs are refused.

For every transfer, both services must return the same successful receipt, with
matching transaction/block/log identities. Its block must be canonical and no
later than the finalized checkpoint. The receipt's collateral logs must exactly
match the queried logs, and starting balance plus net transfers must equal the
ending balance. Deposits and withdrawals that net to zero remain two visible
movements. Self-transfers are deduplicated across incoming/outgoing queries.

The observation may last up to 120 seconds, checked between chunks/receipts; each
HTTP request also has a 15-second timeout. This is a historical-evidence budget,
not a financial approval lifetime. The separate 15-second account preflight and
60-second plan expiry are unchanged. Future integration must reconcile the
uncovered interval before any financial action and persist anchors atomically
with the associated inventory/cash state. A long outage must not reset the anchor.
Requests are paced to at most four starts per second per service by default.
HTTP 429 is returned as unavailable evidence, with Retry-After when provided;
there is no automatic retry or change of VPN exit.

Tests run without network requests or real credentials. They cover wrong chain,
provider disagreement, stale/fake checkpoints, unsuccessful/noncanonical
receipts, offsetting external transfers, incomplete histories, chunk boundaries,
API/response limits and forbidden broadcast methods.

Before financial installation, finish: durable anchors, approved-order event and
position attribution for both position protocols, external-movement policy,
overlap/late confirmation handling, restart/restore, operator alerts, exits and
redemption. A successful deposit receipt is not a successful bot order fill.

Primary references, consulted 17 October 2026:

- https://docs.polymarket.com/resources/contracts
- https://docs.polymarket.com/resources/onchain-position-data
- https://docs.polymarket.com/concepts/pusd
- https://eips.ethereum.org/EIPS/eip-20
- https://ethereum.org/developers/docs/apis/json-rpc/
- https://docs.polygon.technology/pos/reference/rpc-endpoints
- https://docs.tenderly.co/node-rpc/rpc-reference
- https://docs.tenderly.co/node-rpc/overview
- https://drpc.org/docs/polygon-api
