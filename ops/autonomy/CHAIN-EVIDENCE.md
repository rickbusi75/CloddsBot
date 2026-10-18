# Independent collateral evidence — preparation, not financial readiness

`live-chain.cjs` reads Polygon JSON-RPC only. It has no private-key input, signer,
transaction broadcaster, token approval, ledger mutation or exchange order path.
`live-chain-guard.cjs` now connects this evidence to the manual ledger/transport
inside the draft. These components are still not installed in the running service.
Neither an account balance nor a successful chain probe proves
that the bot can trade, exit, redeem or make a profit.

The pUSD address is pinned from Polymarket's current contracts reference:
`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, chain 137, six decimals.
CTF and Protocol V2 position systems are separate; this component verifies
collateral and receipt data, not ERC-1155 position ownership or OrderFilled
attribution by itself. The guard adds those checks for the selected/known assets.

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

The guarded operator initializes a wallet-bound anchor only during explicit owner
activation. Before reservation and before SENDING, a quiet audit must find no
collateral movement, including offsetting transfers. The point advances in the
same SQLite transaction as its corresponding intent transition. Direct ledger
operations on a guarded baseline fail without a matching proof. An older ledger
without a verified anchor requires manual review and cannot be silently adopted.

For a fill, the verified receipt must include the exact approved order hash,
wallet, side, position ID and amounts in both OrderFilled and OrdersMatched.
Only the expected exchange is accepted. ERC-1155 TransferSingle/TransferBatch and
independent historical/current balanceOf reads must agree with the resulting
inventory. Unowned receipt movements cannot become profit. Cash, inventory,
trade-history fingerprint, chain anchor and private provenance events commit
together; a failed inventory check rolls all of them back.

The event interface was checked against the official CTF Exchange V2 sources and
the verified ExchangeV3 implementation ABI, cached with provenance in
`tests/fixtures/polymarket-chain-events.json`. CTF standard/negative-risk and
Protocol V2 use their respective exchange and ERC-1155 manager addresses.
The guard does not infer ownership from an exchange acknowledgement alone.

`reconcile` with no pending intent performs a quiet synchronization only. It can
keep a verified cursor current without submitting an order, resetting capital or
resuming stopped entries. A pending intent prevents that cursor shortcut. Snapshot
tests restore the protocol, anchor and uncertain reservation, then reconcile it
without replay. Read-only status labels its CLOB-only scope and the need for a
fresh chain preflight when the ledger is guarded.

Tests run without network requests or real credentials. They cover wrong chain,
provider disagreement, stale/fake checkpoints, unsuccessful/noncanonical
receipts, offsetting external transfers, incomplete histories, chunk boundaries,
API/response limits and forbidden broadcast methods.

Before financial installation, finish: execution/secret isolation from the main
AI process, the interval after the common finalized block, independent coverage
of unexpected position transfers, outside-capital adjustments, long-outage cursor
recovery, operator alerts, resolved-position valuation and redemption. Actual
filled exchange tests and exits remain unverified. The compiled main agent has
an exec_python handler that inherits process.env; tool availability in an actual
session has not been tested, and that handler was not executed by this audit.
Private SSH/TTY prompts alone are not a boundary against another process that
can access financial secrets. A successful deposit receipt is not a bot fill test.

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
- https://github.com/Polymarket/ctf-exchange-v2/blob/main/src/exchange/interfaces/ITrading.sol
- https://sourcify.dev/server/v2/contract/137/0x641b40ec414a076b9e79E703Fc7BF4EBEC248Bb7?fields=abi
- https://eips.ethereum.org/EIPS/eip-1155
