# Rick's CloddsBot fork

This fork keeps upstream main intact and develops the deployable edition on `rick`.

- Upstream: https://github.com/alsk1992/CloddsBot
- Fork: https://github.com/rickbusi75/CloddsBot
- Production branch: `rick`; develop each change on a feature branch and merge a tested PR.
- Existing ARM64 deployment: `Dockerfile.lab` and `deploy/oracle/`.
- Secrets, wallet files, personal access notes, databases and backups must never be committed.

## OpenRouter

In WebChat settings, choose OpenRouter, enter its API key and select or type the complete provider/model ID. The public catalog labels models supporting tools; manual model entry remains possible if the catalog is unavailable. Model availability, account permissions, tool support and costs still depend on the chosen model.

The main agent, its subagents and conversation summaries use the native OpenRouter Messages endpoint. Tool calls/results, images and incremental SDK streaming retain their structured format. API keys stay masked in settings and are persisted to the private environment file. No fallback to a different paid provider is triggered when the selected provider fails. Credential/provider changes apply on the next request; existing in-flight requests finish with their original settings.

Configuration:

```dotenv
CLODDS_AI_PROVIDER=openrouter
OPENROUTER_API_KEY=
CLODDS_MODEL=provider/model
```

For direct Anthropic use `CLODDS_AI_PROVIDER=anthropic` and its own key. `/model provider/model` changes a conversation's model; the settings model is the global default. Legacy auxiliary Claude IDs use the selected OpenRouter model unless already qualified.

Tests mock network responses and credentials, including tool round trips and native streaming. They do not consume API credits. A paid end-to-end test with a real key is still necessary after account configuration.

## Bring upstream updates into the fork

Work in a development checkout, never in the live container or with production wallet credentials:

```bash
git remote add upstream https://github.com/alsk1992/CloddsBot.git # once per fresh clone
git fetch upstream
git switch main
git merge --ff-only upstream/main
git push origin main
git switch -c update/upstream-YYYY-MM-DD origin/rick
git merge main
```

Resolve any conflicts, run type checking and relevant tests, build ARM64, then open a PR into `rick`. Use `git merge --abort` to abandon an unresolved integration. Avoid force pushes and copying files over our branch: the merge preserves each side's history. Review upstream changes before running them.

After merging the PR, tag the tested revision and build a uniquely tagged Docker image. Back up application data before deployment. Update both direct/VPN Compose image references together. Recreate only CloddsBot, check health, WebChat, model selection and egress. Keep the preceding image tag and backup for rollback. Never deploy `latest` automatically.

## Validate changes

```bash
npm ci --legacy-peer-deps
npm run typecheck
node --test --import tsx tests/providers/openrouter.test.ts
npm run build
```

The original upstream CI/security workflows remain available. The fork's focused checks run for feature PRs into `rick` and include regression tests for adapted features. Known upstream dependency advisories are documented separately; a successful feature check does not mean a clean security audit.
Run deploy/oracle/render.sh on the VPS to render root Compose files from the versioned templates. Keep the previous rendered files for rollback.

Selected fixes retain their upstream authors: degan6 Gamma research/lookup, Tong-bit-art futures market valuation (PR #128), banned2054 cancelled-stop race (PR #129). These fixes do not constitute complete financial validation.
