# Research Hub contributor instructions

Read README.md and HANDOVER.md in this directory before editing. On Rick's
Windows workspace, also read the parent project AGENTS.md, ETAT-REPRISE.md and
docs/SUIVI-AVANCEMENT.md. Before host administration, read the master instructions
in C:/Users/Rick/Documents/Vps oracle.

This module currently implements paper research and read-only account monitoring.
Do not present the existing bot's live-mode setting as evidence that this module
has a live execution adapter. Keep observations, tests and deployed state distinct.

Preserve the shared model proxy, budget accounting, evidence validation, report
deduplication and pause/resume semantics. Keep secrets out of workflow exports,
fixtures, reports and Git. Match dependency/runtime versions to the actual ARM64
deployment. Do not overwrite historical bot dist overlays through a full rebuild.

Work in the task branch; inspect git status first. For code changes run the
relevant unit/integration checks from the README in an isolated Linux environment.
Do not import workflows while an owned execution is active. Do not run a business
workflow for a documentation-only change. Record the changed files, validation,
deployment status, remaining work and rollback context in the current tracker.

HANDOVER.md travels with Git; the parent project's dated operational documents
remain the detailed reference on Rick's machine. A documentation commit is not
a deployment. Never copy private configuration into this portable note.
