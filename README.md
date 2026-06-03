# MCP Données ouvertes France

Serveur MCP qui expose des APIs officielles françaises en open data :

- Annuaire des entreprises : `recherche-entreprises.api.gouv.fr`
- Géocodage : `data.geopf.fr/geocodage`

Par défaut, aucune clé API n'est requise.

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

### 3. Connecter à Claude

Dans les paramètres MCP de Claude.ai, ajouter :

```
URL : https://ton-service.railway.app/mcp
Nom : annuaire-entreprises-fr
```

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
