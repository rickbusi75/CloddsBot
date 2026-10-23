# Research Hub — handover, 23 October 2026

This file travels with the Git repository. Rick's complete current project
documents live in `C:/Users/Rick/Documents/CloudBot VPS`: `AGENTS.md`,
`ETAT-REPRISE.md`, `docs/CAHIER-DES-CHARGES.md`, and
`docs/SUIVI-AVANCEMENT.md`. Those files contain dated operational evidence;
this repository note does not replace a fresh inspection of the instance.

## Scope and deployment snapshot

Research collection, shared evidence memory, native n8n agents, account reading,
evaluation, and one daily Telegram report. Python 3.12, PostgreSQL 17, ARM64.
At the last read-only inspection on 23 October, 19:16 Europe/Paris:

- Hub 0.3.2, image `lab/research-hub:20261023.5`, healthy.
- Bot `lab/cloddsbot:rick-20261023.2-digest`, healthy; worker-only overlay.
- n8n and external runners 2.44.6; ten project workflows active.
- Deployed implementation baseline: `5bdadfd9333be6afd2f069e0c2b63da82f4f9d1f`.
  A later documentation commit does not imply an image rebuild or deployment.
- New research chain: `paper`, `live_dispatch=false`, no live order adapter.
  Existing bot's `DRY_RUN=false` and USD 200 per-order ceiling are separate.
- Rick has expressed and authorized the intended real-money use. That intent
  is recorded separately from technical readiness; this handover does not
  implement or provide instructions for placing bets.

The module is developed in `feat/multi-theme-research-hub`, draft
[PR #15](https://github.com/rickbusi75/CloddsBot/pull/15) targeting `rick`.
The separate Windows `code/` checkout has 33 pre-existing modified paths and
must not be reset or treated as the deployed image. Prototype PR #12 is distinct
and not installed.

## Code map

| File | Responsibility |
|---|---|
| [workflows.py](workflows.py) | Ten native n8n workflows, agent and tool wiring |
| [hub/server.py](hub/server.py) | Private HTTP API, job and model proxy routes |
| [hub/engine.py](hub/engine.py) | Research lifecycle, validation, shared control |
| [hub/store.py](hub/store.py) | PostgreSQL persistence |
| [hub/collectors.py](hub/collectors.py) | Source adapters and collection |
| [registry.json](registry.json) | Source descriptions, key names, documentation URLs |
| [hub/evidence.py](hub/evidence.py) | Subject matching and traceable evidence filters |
| [hub/budget.py](hub/budget.py) | Shared SQLite reservations, proxy, provider backoff |
| [hub/accounting.py](hub/accounting.py) | Read-only account normalization and unavailable-data handling |
| [hub/reporting.py](hub/reporting.py) | Daily report, delivery state and deduplication |
| [digest_owner.py](digest_owner.py) | Existing digest ownership migration/rollback helper |
| [backup.py](backup.py) | Logical PostgreSQL backup |
| [test_hub.py](test_hub.py), [test_integration.py](test_integration.py) | Unit and isolated PostgreSQL validation |

## Configuration and observations

Default model: **OpenRouter `z-ai/glm-5.3-flash`**, verified on Hub, bot and both
n8n workflows containing model nodes. The OpenAI-compatible n8n node uses the
Hub proxy credential; it does not require a separate OpenAI account. The model
node serves the connected orchestrator, specialist and critic. Existing shared
limits are USD 0.60/day, USD 20/month, USD 0.05/call.

At the snapshot: 16,800 discovered markets, pagination incomplete, 33 registered
sources. Hub evaluation had two tracked forecasts and none resolved; no Hub
Brier score was available. Observer history is a different dataset. Source
configuration, enabled status, successful access and relevance are separate facts.

Optional missing access values: EIA, Congress, ACLED, UCDP, ReliefWeb, CoinGecko
Demo and football-data. Open-Meteo remains disabled pending usage qualification.
ACLED access-token renewal is not implemented. No secret values belong here.

Private host paths: `/srv/lab/projects/research-hub`,
`/srv/lab/config/research-hub`, `/srv/lab/data/research-hub`.
The shared observer ledger lives under
`/srv/lab/data/cloddsbot/autonomy/observer.sqlite`.
Use the existing Windows lab SSH key and `known_hosts`, strict host verification,
and localhost tunnels. Never export raw credentials or complete container env.

## Proven behavior and compatibility traps

- 23 Hub unit tests, 25 PostgreSQL tests and 21 observer compatibility tests
  passed on ARM64 for the deployed baseline. No tests were rerun for this
  documentation-only change. Research Hub and Rick edition CI passed for that
  baseline; Security CI still reports inherited JavaScript advisories.
- Native n8n execution 252 completed at 18:43 Paris: tool calls, critic, final
  decision persistence, completion. An incomplete proposal became WAIT with
  its reason retained; no order was created. Re-read after Hub recreation passed.
- Agent node **2.2** is intentional. Tested 3.1 called tools but supplied empty
  tool content to the model. Do not change versions on the assumption that a
  newer node is compatible.
- The final decision tool takes serialized JSON in the string `decision_json`.
  A direct object argument failed native n8n validation. A staged decision is
  immutable and survives truncated closing prose; completion still revalidates.
- Shared pause/resume was tested and original state restored. Monitoring and
  reports continue while new research is paused.
- Provider 429 cooldown and conservative budget reservations are persistent.
  Do not add an unaccounted alternative model-call path.
- Worker-only bot overlay preserves compiled `/app/dist`, including historical
  Metaculus changes. The dependency Git SHA alone does not identify all runtime code.

## Daily report and recovery

Digest owner is `research-hub`; daily time is **20:00 Europe/Paris**. n8n retries
during that hour are deduplicated by the persisted date key. First confirmed
delivery: 23 October 17:51 Paris, Telegram message 31, `daily:2026-10-23` sent.
Do not clear the key to test another delivery. Legacy observer 09:00/20:00
digests and the native DailyDigest were superseded; technical alerts remain.

Verified full archive: `lab-20261023T165317Z.tar.gz`; checksum and isolated
restore passed. Restored 15,600 markets, 371 evidence items, 33 sources, the final
decision and delivered report; the active service later contained more markets.
Use the logical custom-format PostgreSQL dump, not a copy of live physical files.
A complete VPS reboot/recovery test has not been established.

## Remaining work relevant to data quality and operations

Full read-only account reconciliation of deposits, withdrawals, fees and net
results remains incomplete. Other open items: next-day report observation,
inherited security advisories, source freshness, pagination coverage, citation
matching quality, release calendar, review on new evidence, forecast evaluation
after resolutions, and matching the conservative cost ledger to provider usage.
See the complete Windows task tracker for acceptance criteria and dated progress.

Read the [module README](README.md) before any maintenance. Its installation
commands describe available tooling, not a sequence to rerun on a live instance.
Workflow import changes published definitions and deactivates owned workflows;
do not import during an active execution. Integration tests use a separate,
fixed validation database and refuse a pre-existing one. No business workflow
needs to be executed to validate a documentation-only change.
