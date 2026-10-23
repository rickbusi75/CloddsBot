---
name: n8n
description: "Manage the local n8n automation instance: list, create, update, activate workflows and inspect executions via the n8n REST API"
command: n8n
emoji: "🔗"
gates:
  envs:
    - N8N_API_KEY
---

# n8n Integration

CloddsBot peut piloter l'instance n8n du labo (automatisations, veille, webhooks) via son API REST v1.

## Commands

```
/n8n                        Aide
/n8n status                 Vérifie la connexion à l'API n8n
/n8n list [limit]           Liste les workflows (id, nom, actif/inactif)
/n8n get <id>               Détail complet d'un workflow
/n8n create <json>          Crée un workflow (name, nodes, connections)
/n8n update <id> <json>     Met à jour un workflow
/n8n activate <id>          Active un workflow
/n8n deactivate <id>        Désactive un workflow
/n8n run <id>               Déclenche une exécution manuelle
/n8n executions [limit]     Dernières exécutions avec statut
```

## Examples

```
/n8n status
/n8n list 50
/n8n create {"name":"Veille test","nodes":[],"connections":{}}
/n8n activate 123
/n8n executions 20
```

## How It Works

1. **API** : appels HTTP signés avec l'en-tête `X-N8N-API-KEY` vers `N8N_BASE_URL` (défaut `http://172.17.0.1:5678/api/v1`).
2. **Config** : `N8N_API_KEY` et `N8N_BASE_URL` dans `bot.env`.
3. **Agent autonome** : l'agent dispose aussi de l'outil `n8n_api` (method, path, body) pour des appels précis — par exemple construire un workflow complet à partir d'une description en langage naturel, puis l'activer.

## Safety

- La création/modification de workflows n8n ne touche jamais aux fonds ni aux ordres de trading.
- Les clés n8n ne sont jamais affichées dans les réponses.
- Les endpoints `credentials` ne renvoient que des noms/types, jamais de valeurs.
