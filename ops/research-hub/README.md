# Recherche multi-thèmes — n8n + CloddsBot

Service de collecte et mémoire PostgreSQL, dix workflows natifs n8n, orchestrateur
avec spécialiste et contradicteur, portefeuille simulé et lecture du compte réel.
Le budget LLM réutilise le journal SQLite de l'observer et du chat existants.

Les agents utilisent la version de nœud native 2.2 disponible dans n8n 2.44.6.
Le parcours testé avec Agent 3.1 exécutait les outils mais présentait leurs
retours comme vides au modèle ; la compatibilité réelle prime sur le numéro de
version de nœud. Les HTTP tools utilisent n8n-nodes-base.httpRequestTool.

## Déploiement préparé

Sur l'hôte ARM64, en root, depuis ce dossier installé sous
`/srv/lab/projects/research-hub` :

```sh
python3 deploy.py initialize
docker compose build hub
docker run --rm --network none lab/research-hub:20261023.1 python -m unittest -v test_hub
docker compose up -d db
docker run --rm --network research-hub_storage --env-file /srv/lab/config/research-hub/hub.env lab/research-hub:20261023.1 python -m unittest -v test_integration
docker compose up -d hub
python3 deploy.py import
```

`import` crée ou actualise seulement les dix workflows identifiés dans l'état
privé ; il les laisse inactifs. `activate` les actualise puis les active. Ne pas
réimporter pendant une analyse active : la version publiée changerait en cours
d'exécution. La suite d'intégration crée et supprime uniquement une base nommée
`research_validation` et refuse de remplacer une base de ce nom préexistante.

## Accès et configuration

Interface privée : <http://localhost:8766>, via tunnel SSH. Elle distingue compte
réel, simulation, état des sources et couverture incomplète de la pagination.
Les jobs POST exigent le token privé ; aucun token dans les exports de workflow.

Configuration : `/srv/lab/config/research-hub/settings.json` et `hub.env`.
Ne jamais mettre ces fichiers dans Git ou un rapport. Après modification du
fichier env, recréer uniquement hub avec `docker compose up -d --no-deps hub`.

Sous PowerShell, l'assistant masque la saisie et l'envoie par SSH sans fichier
local ni argument de commande contenant la clé :

```powershell
.\Configurer-API.ps1
.\Configurer-API.ps1 -Api EIA_API_KEY
```

Sources configurables : EIA, Congress.gov, ACLED OAuth access token, UCDP,
ReliefWeb appname, CoinGecko Demo et football-data. L'enregistrement ne prouve
pas la validité externe ni les droits d'accès. Le renouvellement automatique du
token ACLED n'est pas encore implémenté. Open-Meteo reste désactivé tant que
l'utilisation finale n'est pas qualifiée.

## État et limites

La nouvelle chaîne est en **paper**. Elle ne copie pas la clé de signature du
bot et n'a pas d'adaptateur d'ordres réels. Le DRY_RUN=false du bot existant n'est
pas modifié. Le réel autonome, les sorties et remboursements réels, le résultat
net réel rapproché restent des travaux à terminer ; les tests simulés ne les
valident pas. La revue horaire de positions simulées réutilise la thèse d'entrée
et ne permet que maintien ou vente de l'issue détenue. Elle ne gère pas encore
les positions réelles ni les déclenchements sur publication nouvelle.

Le digest neuf est désactivé par défaut pour préserver les bilans existants
9 h/20 h. Le passage au bilan consolidé unique nécessite une reprise explicite
de sa propriété et un test d'envoi ; ne pas activer deux rapporteurs.

Les sources marquées « configurées » ou « en attente » ne sont pas toutes
validées. Les adaptateurs de recherche sont appelés à la demande. Le calendrier
complet et l'historique des mouvements réels ne sont pas encore disponibles.

## Sauvegarde et retour arrière

`backup.py` produit un dump logique cohérent au format PostgreSQL custom dans
`/srv/lab/dumps/research-hub/research.dump`. Le répertoire physique postgres doit
être exclu de l'archive générale ; conserver le dump et la configuration privée.
La restauration se vérifie dans une base distincte, jamais en écrasant la base
active. Une désactivation des nouveaux workflows et un arrêt de ce Compose
laissent les services CloddsBot, VPN, n8n et Freqtrade en place.
