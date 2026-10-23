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
docker run --rm --network none lab/research-hub:20261023.4 python -m unittest -v test_hub
docker compose up -d db
docker run --rm --network research-hub_storage --env-file /srv/lab/config/research-hub/hub.env lab/research-hub:20261023.4 python -m unittest -v test_integration
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

## Décisions, preuves et disponibilité du modèle

L'orchestrateur possède un outil final `/v1/research/decision`, absent des
spécialistes. Il conserve une décision validée et immuable avant la clôture du
workflow, sans créer d'ordre ni de prévision. `/v1/research/complete` termine
ensuite le dossier et applique les mêmes contrôles de preuve, d'expiration et
de pause. Une réponse finale tronquée ne détruit donc pas une décision déjà
enregistrée. Les erreurs de validation restent des erreurs avec leur code
précis ; elles ne sont pas présentées comme des abstentions réussies.

Le rapprochement documentaire privilégie les sujets nommés et exclut les mots
génériques des marchés et des dates. Les termes ayant produit le rapprochement
sont conservés dans le dossier. Il s'agit d'un filtre lexical explicite, pas
d'une garantie d'équivalence sémantique ou de véracité du document.

Le proxy autorise le repli entre fournisseurs du même modèle, sous les plafonds
de prix et les réservations existants. Un HTTP 429 final crée une attente
persistante de cinq minutes, respectée avant tout nouvel appel du hub ou
nouveau dossier. Aucun nouveau fournisseur de modèle n'est ajouté à la
configuration. Les recherches sans résultat sont aussi mises en cache.
La sortie du modèle dispose de 8 192 tokens au maximum, avec effort de
raisonnement faible lorsque le fournisseur le supporte. Les réservations et
plafonds monétaires restent appliqués à toute cette sortie, raisonnement inclus.

Le compte distingue solde total et disponible après les achats ouverts,
hors frais. Une valeur de solde ou de position inconnue déclenche un échec de
lecture ; elle n'est pas convertie en zéro. Les anciennes données restent
visibles comme non confirmées. Le résultat net réel demeure incomplet.

## État et limites

La nouvelle chaîne est en **paper**. Elle ne copie pas la clé de signature du
bot et n'a pas d'adaptateur d'ordres réels. Le DRY_RUN=false du bot existant n'est
pas modifié. Le réel autonome, les sorties et remboursements réels, le résultat
net réel rapproché restent des travaux à terminer ; les tests simulés ne les
valident pas. La revue horaire de positions simulées réutilise la thèse d'entrée
et ne permet que maintien ou vente de l'issue détenue. Elle ne gère pas encore
les positions réelles ni les déclenchements sur publication nouvelle.

Le digest neuf est désactivé sur une installation vierge. Le transfert vers le
bilan quotidien unique de 20 h Paris s'effectue après déploiement du worker
compatible :

```sh
python3 digest_owner.py activate
python3 digest_owner.py status
```

Le helper vérifie le hash du worker, arrête brièvement l'observer pendant le
transfert, désactive le digest natif, puis active le hub pour le propriétaire
Telegram existant. Les anciens créneaux 9 h/20 h et les anciens digests en
attente sont ignorés par le worker ; ses alertes de panne restent actives.
Un bilan déjà acquitté par l'observer le soir du transfert n'est pas renvoyé.
La confirmation Telegram et son message_id sont conservés ; un envoi dont
l'accusé est perdu n'est pas réémis aveuglément. Les 429 confirmés restent
réessayables aux passages suivants du workflow.

`python3 digest_owner.py rollback` restaure le responsable et les réglages
précédents depuis l'état privé. Il n'altère pas les réglages de trading.
L'image du bot utilisée pour ce transfert hérite de rick-20261023.1 et ne
remplace que ops/autonomy/worker.py ; ne pas reconstruire son dist depuis le
clone en perdant les overlays déployés.

Les commandes Telegram `/veille pause` et `/veille reprise` pilotent le
contrôle partagé des deux veilles. La pause bloque les nouvelles recherches
et entrées, tout en maintenant rapprochement, résolution et bilans. Une entrée
proposée par une analyse déjà lancée est également bloquée après la pause.

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
