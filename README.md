# MCP Données ouvertes France

Serveur MCP qui expose des APIs officielles françaises en open data :

- Annuaire des entreprises : `recherche-entreprises.api.gouv.fr`
- Géocodage : `data.geopf.fr/geocodage`

Par défaut, le serveur est public sans authentification entrante. Voir
[Authentification entrante](#authentification-entrante-optionnelle) pour le
protéger par identifiant/mot de passe ou par token. Les clés des services tiers
restent optionnelles selon les outils utilisés.

## Outils disponibles

| Outil | Description |
|---|---|
| `search_entreprise` | Recherche par nom + ville/CP optionnel |
| `get_entreprise_by_siren` | Fiche complète par numéro SIREN |
| `enrich_batch` | Enrichissement en lot (max 20 entreprises par appel) |
| `geocode_address` | Géocode une adresse, un lieu, ou une parcelle |
| `reverse_geocode` | Retrouve l'adresse/le lieu le plus proche de coordonnées GPS |

## Géocodage

Le géocodage France utilise le service de géocodage de la Géoplateforme :

- URL directe : `https://data.geopf.fr/geocodage/search`
- URL inverse : `https://data.geopf.fr/geocodage/reverse`
- Sources : BAN, BD TOPO et Parcellaire Express selon les index demandés

Exemples d'usage MCP :

```json
{
  "adresse": "10 avenue des Champs Elysees",
  "ville": "Paris",
  "code_postal": "75008"
}
```

```json
{
  "latitude": 48.8566,
  "longitude": 2.3522
}
```

### Géocodage international optionnel

Le serveur peut appeler un service compatible Nominatim pour du géocodage mondial, mais il est désactivé par défaut. Configurez explicitement :

```bash
NOMINATIM_BASE_URL=https://votre-service-nominatim.example
NOMINATIM_USER_AGENT="mcp-datagouv/1.0 contact@example.com"
```

Puis appelez `geocode_address` ou `reverse_geocode` avec `"source": "global"`.

Note : le service public `nominatim.openstreetmap.org` impose des limites fortes et ne doit pas être utilisé comme géocodeur générique sans décision explicite et respect de sa politique d'usage.

## Authentification entrante (optionnelle)

Par défaut le serveur est **authless** : n'importe qui connaissant l'URL peut appeler les outils. Pour le protéger, deux méthodes sont acceptées simultanément.

| Variable | Rôle |
|---|---|
| `MCP_USERNAME` | Identifiant HTTP Basic |
| `MCP_PASSWORD_SHA256` | Empreinte sha256 (64 caractères hex) du mot de passe — **recommandé** |
| `MCP_PASSWORD` | Mot de passe en clair (alternative ; ignoré si l'empreinte est définie) |
| `MCP_TOKEN` | Token statique accepté en `Authorization: Bearer <token>` ou `x-api-key: <token>` |
| `MCP_AUTH_REALM` | Realm annoncé dans `WWW-Authenticate` (défaut : `donnees-ouvertes-france`) |

Générer l'empreinte :

```bash
printf '%s' 'ton-mot-de-passe' | shasum -a 256
```

Règles de comportement :

- Aucune variable définie → serveur authless, avertissement au démarrage.
- `MCP_USERNAME` sans mot de passe (ou l'inverse) → **le serveur refuse de démarrer**, pour éviter une exposition silencieuse.
- `/health` reste toujours public : le healthcheck Railway n'envoie pas d'identifiants.
- Les comparaisons se font en temps constant ; un mauvais identifiant coûte le même temps qu'un mauvais mot de passe.
- Un refus renvoie `401` avec les challenges `Basic` et `Bearer`.

Test en local :

```bash
export MCP_USERNAME=romus
export MCP_PASSWORD_SHA256=$(printf '%s' 's3cr3t' | shasum -a 256 | cut -d' ' -f1)
python server.py

curl -u romus:s3cr3t -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

### Compatibilité des clients MCP

L'authentification statique fonctionne avec les clients qui permettent d'ajouter
un en-tête HTTP personnalisé. Exemple avec un token :

```bash
claude mcp add --transport http datasiren https://ton-service.railway.app/mcp \
  --header "Authorization: Bearer $MCP_TOKEN"
```

Cette branche n'implémente pas OAuth. Un client qui exige la découverte et le
flux OAuth MCP standard ne pourra donc pas utiliser l'authentification statique
ci-dessus ; laissez le serveur authless ou ajoutez une implémentation OAuth.

## Complément Sirene INSEE

L'API Sirene open data de l'INSEE peut compléter ce MCP pour des recherches plus proches du répertoire Sirene officiel : unités légales, établissements, données historisées, liens de succession. Elle demande toutefois un compte et impose une limite open data de 30 requêtes/minute. Le MCP actuel garde donc `recherche-entreprises.api.gouv.fr` comme source sans clé, et peut recevoir un connecteur Sirene dédié plus tard si des identifiants INSEE sont disponibles.

## Déploiement sur Railway

### 1. Prérequis
- Compte [Railway](https://railway.app) (plan gratuit suffisant)
- Git
- Python 3.12 pour l'utilisation locale hors Docker

### 2. Déployer

```bash
# Clone / init le repo
git init
git add .
git commit -m "init mcp annuaire entreprises"

# Déployer sur Railway
railway login
railway init
railway up
```

Ou via l'interface Railway : **New Project → Deploy from GitHub repo**.

Railway détecte automatiquement le Dockerfile et expose le service sur une URL publique du type `https://xxx.railway.app`.

Le serveur utilise automatiquement la variable Railway `RAILWAY_PUBLIC_DOMAIN`
pour autoriser son hostname sans le coder en dur. Variables optionnelles :

| Variable | Rôle |
|---|---|
| `MCP_ALLOWED_HOSTS` | Hostnames supplémentaires, séparés par des virgules (domaines personnalisés) |
| `MCP_ALLOWED_ORIGINS` | Origins navigateur autorisées, séparées par des virgules |
| `MCP_SERVER_VERSION` | Version annoncée aux clients MCP (défaut : `1.0.0`) |

### 3. Connecter un client MCP

Ajouter un serveur MCP distant avec :

```
URL : https://ton-service.railway.app/mcp
Nom : donnees-ouvertes-france
```

Le point d'entrée doit être l'URL HTTPS terminée par `/mcp`. Si l'authentification
est active, configurez le client avec HTTP Basic, `Authorization: Bearer ...` ou
`x-api-key`. Le client doit permettre les en-têtes personnalisés.

## Données retournées

Pour chaque entreprise, le MCP retourne :

- SIREN / SIRET
- Nom complet et sigle
- Forme juridique
- Date de création
- Tranche d'effectifs
- Code NAF + libellé activité
- Adresse complète du siège (avec coordonnées GPS)
- Liste des dirigeants (nom, qualité, année de naissance partielle)
- Chiffre d'affaires et résultat si disponibles

Pour chaque résultat de géocodage, le MCP retourne :

- Libellé normalisé
- Latitude / longitude WGS84
- Score de correspondance
- Type de résultat (`housenumber`, `street`, `locality`, `municipality`, etc.)
- Composants d'adresse disponibles
- Propriétés brutes de la source pour audit

## Utilisation locale (test)

```bash
pip install -r requirements.txt
python server.py
# Serveur Streamable HTTP disponible sur http://localhost:8000/mcp
```
