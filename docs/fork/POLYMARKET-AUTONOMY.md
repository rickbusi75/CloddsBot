# Autonomous observer and paper portfolio

The Oracle deployment can run public research and paper accounting independently of
the Windows PC. This is an experimental observer, not a validated profitable strategy.
It has no live execution or signing code. Existing trading integrations remain separate.

## Operation

- clodds-observer.service starts at VPS boot and restarts after failure or bot recreation.
  It runs Python in the existing bot container, using its existing network.
- One hourly scan, at most 100 high-volume markets and two analyses per scan. Binary
  Yes/No markets only, liquidity and daily volume at least 1,000, resolution in 12 hours
  to 45 days, indicative prices between 0.08 and 0.92. All categories are eligible.
- Brave retrieves current sources; up to three public pages are read. Pages are untrusted
  data. Manifold forecasts provide context; they do not count as independent evidence.
- The Odds API reads two rotating sports once per UTC day, one region and h2h only.
  No retry storm after a timeout. Calls stop below ten remaining credits. Bookmaker
  probabilities have the margin removed, but mismatched dates/rules are not arbitrage.
- Decisions, source extracts, reasons, counterarguments, costs, positions, runs and
  outgoing notifications persist in /data/autonomy/observer.sqlite.
- French Telegram reports at 09:00 and 20:00 Europe/Paris. Only three consecutive
  failed research cycles trigger an extra daily operational alert. No message per scan.
- /veille statut, /veille bilan, /veille pause, /veille reprise require the
  explicitly allowlisted owner and a private Telegram chat. These commands cost no AI.
  Pause prevents new research and paper orders, preserving accounting and reports.

## Cost control

OpenRouter research and the native CloddsBot agent client share one SQLite cost ledger.
Before each paid request, the software checks the key's reported daily/monthly usage,
current model prices and persistent reservations. It enforces USD 0.60/day, USD 20/month,
and USD 0.05 per request. Provider routing has price ceilings and paid fallback disabled.
Reservations commit before network submission; timeout/crash does not release them.
The observer replaces its reservation with the reported cost when available; native
chat reservations remain conservative, including streaming. This can stop spending
early. A failed lookup blocks spending. Images require a separate estimator and are
blocked by the optional budgeted native client.

Provider-reported usage is added to local reservations, rather than taking their
maximum: provider counters may omit in-flight requests and include other clients.
This deliberately double-counts some settled spending to avoid under-reserving.

Enable the native client guard with CLODDS_AI_BUDGET_ENABLED=true. It protects calls
through createAgentClient, including the main chat and its native assistants. It does
not cap independent scripts, separate provider integrations, other devices using the
same API key, Brave billing, or The Odds API billing. A provider-side monthly key cap is
additional protection where available. No subscription, paid upgrade or automatic
credit replenishment is created.

## Paper portfolio

Initial fictitious capital: **200 pUSD**, not a conversion of EUR 200. Maximum paper
order cost including fee reserve: 5 pUSD; aggregate exposure: 30 pUSD. Stop new entries
after 15 pUSD of gross daily realized losses or equity below 180 pUSD.

A paper entry requires two supplied source URLs on different hosts, model-declared
independent primary evidence, clear resolution rules, and a counterargument. The
effective probability lower bound is no greater than estimated probability minus
0.10; the required advantage is 0.08 after the current market's protocol taker fees. These are
research heuristics, not statistically calibrated probabilities or demonstrated edge.
There is no instruction to force a trade or maximize the number of bets.

Fills use the observed asks, available depth, the exchange minimum size and a snapshot
no older than 60 seconds. Wide/crossed books and unsupported fees abstain. Market and
event deduplication persist across restarts. Holdings mark at observed bid; a fresh
reconciliation timestamp is displayed in status JSON. A closed market is not treated
as a win: settlement requires final resolution and binary payout prices.

Read the CLOB market's fd parameters (rate r, exponent e=1) rather than treating the
legacy fee-rate base_fee field as the current fee schedule. Unknown schedules abstain.
For protocol v2, use Gamma positionIds even when legacy CTF clobTokenIds are also
present; unsupported protocols abstain. See the official
[fee formula](https://docs.polymarket.com/trading/fees) and
[market identifiers](https://docs.polymarket.com/market-data/market-details).

Simulation does not model queue priority, competition between snapshots, real exchange
rejections, on-chain settlement delays, redemption transactions, or all correlations
across different events. It holds to final resolution, with no discretionary early exit.
Paper gains, especially synthetic unit-test gains, are never evidence of live profit.

## Administration and recovery

    sudo systemctl status clodds-observer
    sudo systemctl stop clodds-observer
    sudo systemctl start clodds-observer
    sudo journalctl -u clodds-observer -n 40 --no-pager
    sudo docker exec cloddsbot-bot-1 python3 /app/ops/autonomy/worker.py status
    sudo docker exec cloddsbot-bot-1 python3 /app/ops/autonomy/worker.py pause

No new port or polling bot is needed; Telegram sending reuses the configured bot and
explicit owner. The existing bot is the sole Telegram update consumer. SQLite locking
prevents overlapping worker instances. Notification keys avoid normal retries sending
duplicates; a crash after Telegram accepts a message but before local acknowledgement
can still duplicate a message (Telegram offers no client idempotency key).

Back up the database using SQLite's backup API, not a live WAL file copy. The deployment
extends the existing coherent CloddsBot dump. Rollback: stop the observer, restore the
previous bot image and configuration from the private pre-deployment directory; keep
the paper database for audit. Do not overwrite the main CloddsBot database.

The external clodds-observer-watchdog.timer checks freshness every five minutes on
the VPS host. Three consecutive stale checks send one operational Telegram alert,
and recovery sends one confirmation. This does not require a running bot container
or an AI call. The host itself being offline still requires external monitoring.
ops/autonomy/backup-observer.py creates the coherent dump used by lab-backup.

The watchdog also checks the Gluetun health state in VPN mode. After three failed
checks it can recreate only the CloddsBot VPN/bot pair, at most once per hour,
then restart the observer. It preserves VPN mode and existing relay filters,
never falls back to direct access, and respects a stopped observer or exited bot.
The private deployment uses Rick's requested Madrid relay es-mad-wg-102. A fixed
relay can be unavailable; recovery keeps that choice and reports persistent failure.

## Forecast validation and unavailable position marks

New research records every valid chosen-outcome probability, including abstentions,
with the contemporaneous Gamma indicative price, cutoff, action and model. It never
invents a historical baseline for older research. The forecasts table adds to the
existing journal; all original positions, costs and research remain intact.

An hourly evaluator reads at most ten public markets (15-second request timeout),
sharing reads for repeated forecasts. Pending markets are checked daily before
cutoff and hourly after cutoff, oldest check first. Identity, condition ID, outcome
labels, final UMA resolution and exact binary payouts must agree before scoring.
Missing data remains pending and gets a later check; it is never scored as a loss.

The Brier score is mean (probability - binary result)^2, compared with the same
score for the recorded market price; lower is better. Telegram displays the counts
and both scores once final outcomes exist. Repeated forecasts and correlated events
are not independent samples. These descriptive scores do not demonstrate profit,
validate probability calibration, or automatically unlock real trading.

Each open paper position reconciles independently. An unavailable, stale or wrong
book keeps its last known mark, visibly labels equity as indicative, and blocks new
paper exposure until all open positions can be marked or finally settled. Other
positions continue reconciling. Final payouts match outcome labels and market ID.

## Live trading gate

Rick approved a future EUR 200 capital with limits EUR 5/order, EUR 30 total exposure,
EUR 15 daily realized loss, and small bounded operational tests using the existing
balance. EUR conversion, aggregate account exposure, actual fills/partial fills,
idempotent retries, cancellations, reconciliation after downtime and redemption still
require validation before unattended live trading.

User eligibility is independent of VPS/VPN exit IP. Check the current official
[geographical policy](https://docs.polymarket.com/api-reference/geoblock) and requesting
IP before live tests. Never use network changes to bypass an operator restriction.
Read-only access and a successful signature test do not prove full live trading.
