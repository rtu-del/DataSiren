# MCP Annuaire Entreprises France

Serveur MCP qui expose l'API officielle du gouvernement français (`recherche-entreprises.api.gouv.fr`) — **open data, aucune clé API requise**.

## Outils disponibles

| Outil | Description |
|---|---|
| `search_entreprise` | Recherche par nom + ville/CP optionnel |
| `get_entreprise_by_siren` | Fiche complète par numéro SIREN |
| `enrich_batch` | Enrichissement en lot (max 20 entreprises par appel) |

## Déploiement sur Railway

### 1. Prérequis
- Compte [Railway](https://railway.app) (plan gratuit suffisant)
- Git

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
URL : https://ton-service.railway.app/sse
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

## Utilisation locale (test)

```bash
pip install -r requirements.txt
python server.py
# Serveur SSE disponible sur http://localhost:8000/sse
```
