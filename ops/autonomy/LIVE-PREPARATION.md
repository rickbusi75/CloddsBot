# Live preparation: persistent starter ledger and explicit order confirmation

Status: implementation under validation. This is not a claim of live readiness,
profitability, completed real-money tests, or a replacement for the pre-live audit.
The deployed research worker still has no route to these submission methods.

The initial collateral is approximately 20.28 pUSD. The proposed starter ceilings
are 1.50 pUSD per entry, 5 pUSD aggregate exposure, 2 pUSD daily gross losses and
2 pUSD total drawdown from the initial verified balance, with four entries per UTC
day. Outstanding positions and reservations consume the remaining loss budget;
the effective exposure can therefore be less than the 5 pUSD ceiling. Fees are
reserved before signing. The previously approved EUR 200/5/30/15 framework is not
implicitly substituted for these pUSD bounds or activated by depositing funds.

`live-ledger.cjs` stores intentions and state transitions in a separate private
SQLite file. Activation needs an explicit authenticated owner action and a flat,
freshly checked account. No activation entry point is installed by this change.
Each submission needs the exact plan digest, including asset, side, price,
quantity, fee schedule, resolution-rule hash and expiry. A timeout retains its
reservation, halts entries and cannot trigger another POST. Restart cannot reset
losses or an uncertain submission. Risk-reducing exits can still be explicitly
approved after a halt when the account is reconciled.

`live-reader.cjs` provides read-only exhaustive account checks and previews. It
refuses stale, incomplete, restricted, unknown-fee or unvalued exposure. Keys are
sent only in CLOB authentication headers; redirects are rejected. Binary asset
identifiers follow the market protocol. No model-supplied source classification
can authorize a submission.

`live-transport.cjs` separates signing, one submission and reconciliation. The
signed amounts and EIP-712 hash must match the approved plan. Exchange acceptance
alone cannot release risk. Account inventory and cash, authenticated order data,
and confirmed trades must agree. Cancellation alone does not prove no fill.
Trade-history fingerprints are persisted with the account baseline and each
confirmed fill. Missing/changed history, duplicate trades, unrelated completed
trades and untracked holdings block reconciliation even if cash and exposure
appear unchanged. Account pagination requires an explicit completion cursor.
The reader supports TAKER trades only: a MAKER record is rejected rather than
misattributed to the taker order. Confirmed records require a transaction hash;
this is authenticated API evidence, not an independently verified chain receipt.
Before booking a fill, execution prices, fee-rounded proceeds/cost and received
shares must agree with the approved order and observed balances. Unexpected
cashflows retain the reservation and halt new entries. Equal offsetting external
cashflows cannot be ruled out by balance differences alone; authoritative chain
activity and external-movement handling remain a deployment prerequisite.
The current signer supports only cent prices and two-decimal share quantities;
unsupported precision is rejected rather than silently rounded. Fees withheld
from received shares are recorded as actual net inventory. Small residual shares
must remain tracked; they must not be described as an empty account.

The only CLI supplied here is **read only**:

    node /app/ops/autonomy/live-check.cjs

It does not open a live ledger, activate a mode, create a signature, approve tokens,
send an order, redeem, transfer, or change private configuration. It reports a
filtered account snapshot. Signing/submission classes are library code; only
isolated fixtures and a public dummy key have exercised them so far.

The snapshot helper can now atomically back up both observer.sqlite and live.sqlite
when present. Tests restore a ledger with an outstanding WAL-backed submission,
verify that it stays reserved/unknown after restart, and preserve the previous
good dump when a source is corrupt. Installing the updated helper and excluding
the active live.sqlite/WAL from the host archive still belong to deployment.

Before adding operator controls or enabling an execution service, finish and
review: installation of the separate ledger backup, authoritative handling
of outside cashflows and maker trades, source/strategy validation, settlement and
Deposit Wallet redemption, account-latency and real-exchange tests, exposure and
currency checks, emergency cancellation and notifications. Actual activation and
real-money transactions remain actions for the human operator.

Current isolated ARM64 coverage: 46 tests, including an abrupt SIGKILL with an
outstanding SENDING record, recovery without replay, and simulated purchase/exit
with a deposit that must not be recorded as profit. These tests use no real keys
or network requests and do not validate exchange execution or profitability.

Primary protocol references:

- https://docs.polymarket.com/trading/place-orders
- https://docs.polymarket.com/api-reference/trade/get-single-order-by-id
- https://docs.polymarket.com/api-reference/trade/get-trades
- https://docs.polymarket.com/market-data/market-details
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/api-reference/geoblock
