import { readFile, writeFile, rename, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

/** Local deterministic commands: no AI, no shell, no exchange access. */
export async function observerCommand(text: string, owner: boolean, privateChat: boolean,
  stateDir = process.env.CLODDS_STATE_DIR || process.env.HOME || '.') : Promise<string | null> {
  if (!/^\/veille(?:@\w+)?(?:\s|$)/i.test(text)) return null;
  if (!owner || !privateChat) return 'Commande réservée à Rick en conversation privée.';
  const dir = join(stateDir, 'autonomy');
  const action = text.trim().split(/\s+/)[1]?.toLowerCase() || 'statut';
  if (action === 'pause' || action === 'reprise') {
    await mkdir(dir, { recursive: true });
    const temporary = join(dir, `control-${randomUUID()}.tmp`);
    await writeFile(temporary, JSON.stringify({ paused: action === 'pause', at: new Date().toISOString() }), { mode: 0o600 });
    await rename(temporary, join(dir, 'control.json'));
    return action === 'pause' ? '⏸ Veille et nouvelles simulations suspendues. Suivi des positions et bilans conservés.'
      : '▶ Veille et simulation reprises au prochain cycle horaire. Aucun trading réel.';
  }
  if (action !== 'statut' && action !== 'bilan') return '/veille statut · /veille pause · /veille reprise · /veille bilan';
  try {
    const status = JSON.parse(await readFile(join(dir, 'status.json'), 'utf8'));
    const age = Date.now() - new Date(status.at).getTime();
    const warning = !Number.isFinite(age) || age > 120_000 ? '⚠ État ancien : vérifier le service VPS.\n' : '';
    return warning + String(status.report) + '\nDernier cycle : ' + JSON.stringify(status.lastRun);
  } catch {
    return 'Veille pas encore disponible. Vérifier clodds-observer.service sur le VPS.';
  }
}
