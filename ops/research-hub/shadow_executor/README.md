# Dedicated executor — S1a shadow pilot

This version observes completed research BUY/SELL decisions created after its
installation watermark. It does **not** sign, submit or cancel orders and has
no signing credentials. `live_dispatch=true` or `mode=live` fails at startup.
It cannot be activated for live execution by changing a setting.

Rick delegated the T13 architecture/cap choices on 24 October 2026. Selected:
dedicated service, maximum 2 pUSD per proposed BUY including a conservative fee
reserve, 4 pUSD per Europe/Paris day and 6 pUSD total exposure. Exposure includes
all real positions at the larger of cost or current mark, plus shadow reserves.
Unknown positions/events, active account orders, stale reads, provider errors,
incomplete rules, bad fees and missing control files block proposals. A minimum
share requirement never raises the money cap. SELL is explicitly blocked in S1a.

`shadow_candidate` means a hypothetical proposal, never a real-order approval.
Every row has `live_eligible=false` and `transmitted=0`. No performance or net
profit follows from a candidate. Citation precision, signing, real reconciliation,
SELL and redemption remain open. Shadow reserves have no automatic release:
without real reconciliation it would be unsafe to infer that an exposure closed.
They can therefore exhaust the pilot and stop further proposals.

The only public reads are hard-coded GETs on Polymarket's
[order book endpoint](https://docs.polymarket.com/market-data/prices-order-books)
and market fee metadata. The [fee formula](https://docs.polymarket.com/trading/fees)
is checked against the returned schedule, never assumed from a category label.
The pilot observes up to three new decisions per 60-second pass. It makes no
LLM call and sends no extra Telegram message. The Hub exposes its status and
adds one line to the existing daily report.

## Installation boundary

Build with `Dockerfile.executor`; combine `compose.yaml` and
`compose.executor.yaml` and start only the executor. Configuration lives in
`/srv/lab/config/research-executor/settings.json` and `executor.env` (private).
Only `DATABASE_URL` belongs in the env file, using the separate `research_shadow`
role. No Hub/bot env file, CLOB credentials, provider key or signing secret is
mounted. The observer directory is mounted read-only to read the common control;
mounting the parent preserves atomic file replacements. It contains the existing
observer journal/status too; these are not used as financial authority here.
No host port is published.

The role reads research, market, account and account-error tables, and can
insert its own immutable intent rows and update heartbeat state. It cannot
write the Hub's research/account/order tables, delete intents or alter schemas.
`admin.initialize()` creates the role/schema once using a privileged connection;
it refuses to replace a pre-existing schema or role. Secrets must remain inside
the host configuration, never in command arguments, stdout or Git.

## Tests and backup

Run `python -m unittest -v test_hub test_shadow_executor` in Linux and
`python -m unittest -v test_integration` with the isolated validation database.
The PostgreSQL tests include persistent idempotence, competing transaction locks,
budget reservation and provider failure. The same test database refusal/drop
semantics as the Hub apply.

The schema is inside the research database and is included in its coherent
`pg_dump` and the lab backup; no second financial SQLite ledger is created.
**Roles are global and are not in pg_dump**. When restoring to a new cluster,
recreate `research_shadow` first with `admin.create_role()` and the password
already held in the private executor env file, restore the dump with the normal
owner mapping, then `admin.grant_access()` if ACLs were omitted. Never run
`initialize()` on a restored schema: its watermark and intent history must survive.

Rollback: stop/remove only the executor, restore the prior Hub image/Compose
if needed, and retain the shadow schema/journal and private config as evidence.
Do not clear accepted shadow reserves to manufacture a clean observation period.
