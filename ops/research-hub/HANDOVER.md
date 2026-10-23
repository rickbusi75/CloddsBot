# Research Hub — handover, 23 October 2026 (evening)

This file travels with the Git repository. Rick's complete current project
documents live in `C:/Users/Rick/Documents/CloudBot VPS`: `AGENTS.md`,
`ETAT-REPRISE.md`, `docs/CAHIER-DES-CHARGES.md`, and
`docs/SUIVI-AVANCEMENT.md`. Those files contain dated operational evidence;
this repository note does not replace a fresh inspection of the instance.

## Scope and deployment snapshot

Research collection, shared evidence memory, native n8n agents, account reading,
evaluation, and one daily Telegram report. Python 3.12, PostgreSQL 17, ARM64.
At the last inspections on 23 October (21:04–21:05 Europe/Paris):

- Hub 0.3.2, image `lab/research-hub:20261023.12`, healthy; a **controlled
  full VPS reboot was verified** (boot 20:38 UTC: ten containers healthy,
  digest key, account read, universe cursor, shared control and ten workflows
  persisted, scheduler resumed).
- Bot `lab/cloddsbot:rick-20261023.2-digest`, healthy; worker-only overlay.
- n8n and external runners 2.44.6; ten project workflows active.
- Deployed implementation baseline: **`420f295`** on this branch. Earlier
  commits today: f8cac5e + 5bc4e24 (account net reconciliation and digest
  display), 88104da (40-page pagination, bounded error diagnostics),
  d0883c1 (follow-redirects/toml patches plus justified allowlist — CI
  Security green), 85fd829 (failed AI calls settle at the reserved amount),
  cc54fb8 (subject-anchor filter on the agent's search tool),
  21ebf2a (review-on-new-evidence), dfe930ad (docs).
- New research chain: `paper`, `live_dispatch=false`, no live order adapter.
  Existing bot's `DRY_RUN=false` and USD 200 per-order ceiling are separate.
- Real-path preparation design: isolation, twelve guardrails G1-G12 and
  staged validation S0-S4 documented in Rick's tracker (T13); awaiting
  Rick's architecture and cap decisions. Nothing executes real orders.
- Account reads now include a **residual net reconciliation** from validated
  wallet flows (deposits inferred, `pnl_complete`), displayed in the daily
  digest with an explicit non-profit caveat.
- Evidence quality: the agent's search tool applies the same subject-anchor
  filter as dossier evidence, and `related_evidence` deduplicates one URL to
  one reference (a refetched communiqué is not an independent source).
- A review-on-new-evidence pass (`recheck`) flags BUY/SELL dossiers touched
  by fresher matching publications and surfaces them in the daily report.
- Universe pagination runs 40 pages per 30-minute pass (full cycle in hours
  instead of over a day). GDELT provider throttling (HTTP 429 since 17:03)
  is handled by the documented one-hour deferral.

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

At the 21:04 snapshot: 34,800 discovered markets (pagination cycle open since
10:22 UTC), 33 registered sources. Hub evaluation had four tracked forecasts
and none resolved; no Hub Brier score was available. Observer history is a
different dataset (136 tracked / 17 resolved there — do not mix the two).
Source configuration, enabled status, successful access and relevance are
separate facts. An evidence sample audit on 23 October measured one subject
confusion (caught by the agent, WAIT) and 39 percent URL reuse across
dossiers; both root causes found in code were fixed the same evening.

Optional missing access values: EIA, Congress, ACLED, UCDP, ReliefWeb, CoinGecko
Demo and football-data — each installs with one `Configurer-API.ps1 -Api ...`
command (writes the key, enables the source, recreates the hub). See
`docs/guides/CONFIGURATIONS-RICK.md` in Rick's tracker. Open-Meteo remains
disabled pending usage qualification. ACLED access-token renewal is not
implemented. No secret values belong here.

Private host paths: `/srv/lab/projects/research-hub`,
`/srv/lab/config/research-hub`, `/srv/lab/data/research-hub`.
The shared observer ledger lives under
`/srv/lab/data/cloddsbot/autonomy/observer.sqlite`.
Use the existing Windows lab SSH key and `known_hosts`, strict host verification,
and localhost tunnels. Never export raw credentials or complete container env.

## Proven behavior and compatibility traps

- 27 Hub unit tests and 29 PostgreSQL tests passed on ARM64 for the deployed
  image `.12` (with the reconciliation, budget, filtering and recheck tests);
  21 observer compatibility tests were not rerun for these changes. CI
  Research Hub, Rick edition **and Security** passed on `d0883c1` after the
  dependency patches and justified allowlist.
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
**A controlled full VPS reboot was verified on 23 October (boot 20:38 UTC):
all ten containers, the digest key, the account read, the universe cursor,
the shared control and ten workflows persisted; scheduler resumed at 20:43.**
A fresher checksummed backup `lab-20261023T203538Z.tar.gz` was taken right
before that reboot.

## Remaining work relevant to data quality and operations

Open items: second-day report observation (24 October 20:00 Paris), pagination
cycle completion and residual-category measurement, citation re-measurement
under the URL dedup, forecast evaluation after resolutions, real-path stages
S1-S4 after Rick's architecture decision, and the seven optional source keys
(Rick's configurations). The account net reconciliation and the conservative
cost-ledger matching were delivered today (residual deposits inferred from
validated flows; failed calls settle at the reserved amount with an explicit
`hub_uncertain` state).
See the complete Windows task tracker for acceptance criteria and dated progress.

Read the [module README](README.md) before any maintenance. Its installation
commands describe available tooling, not a sequence to rerun on a live instance.
Workflow import changes published definitions and deactivates owned workflows;
do not import during an active execution. Integration tests use a separate,
fixed validation database and refuse a pre-existing one. No business workflow
needs to be executed to validate a documentation-only change.
