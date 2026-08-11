"""
MCP Server — Donnees ouvertes France
Transport : Streamable HTTP stateless (authless, Claude/ChatGPT compatible)
v8
"""

import os
import time
import httpx
import uvicorn
from typing import Any, Optional
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


def _csv_env(name: str) -> list[str]:
    """Parse a comma-separated environment variable, ignoring empty values."""
    return [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]


def _transport_security() -> TransportSecuritySettings:
    """Build DNS-rebinding allowlists without hard-coding the Railway domain."""
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    railway_public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_public_domain:
        allowed_hosts.append(railway_public_domain)
    allowed_hosts.extend(_csv_env("MCP_ALLOWED_HOSTS"))

    allowed_origins = _csv_env("MCP_ALLOWED_ORIGINS") or [
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://claude.ai",
        "http://127.0.0.1:*",
        "http://localhost:*",
    ]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )


mcp = MCPServer(
    name="donnees-ouvertes-france",
    version=os.environ.get("MCP_SERVER_VERSION", "1.0.0"),
    instructions="""
    French official open data tools for companies, geocoding, public aids, and waste regulation.
    - search_entreprise: find companies by name + optional city/postal code
    - get_entreprise_by_siren: full details by SIREN number
    - enrich_batch: enrich up to 20 companies in one call
    - geocode_address: get coordinates from an address/place/parcel
    - reverse_geocode: get the nearest address/place/parcel from coordinates
    - search_aides_territoires: search public aids for collectivités/porteurs de projets (Aides-Territoires)
    - list_perimetres_at: resolve a region/department/commune name to a perimeter code
    - search_aides_les_aides: search enterprise aids (national + European) via les-aides.fr
    - get_aide_les_aides: get full detail of an enterprise aid from les-aides.fr
    - list_refs_les_aides: list valid reference values for les-aides.fr filters (domaines, filieres, etc.)
    - trackdechets_company: get a company's waste regulatory profile (ICPE, operator types, receipts)
    - trackdechets_eco_organismes: list registered eco-organisms filtered by waste type
    Never hallucinate company, address, or aid data — always use these tools.
    """,
)

# --- Entreprises / Géocodage ---
ENTREPRISES_BASE_URL = "https://recherche-entreprises.api.gouv.fr"
FR_GEOCODING_BASE_URL = os.environ.get(
    "FR_GEOCODING_BASE_URL",
    "https://data.geopf.fr/geocodage",
).rstrip("/")
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "").rstrip("/")
NOMINATIM_USER_AGENT = os.environ.get("NOMINATIM_USER_AGENT", "")
MAX_GEOCODING_RESULTS = 50

# --- Aides-Territoires ---
AT_BASE_URL = "https://aides-territoires.beta.gouv.fr/api"
AIDES_TERRITOIRES_TOKEN = os.environ.get("AIDES_TERRITOIRES_TOKEN", "")

# --- les-aides.fr ---
LES_AIDES_BASE_URL = "https://api.les-aides.fr"
LES_AIDES_API_KEY = os.environ.get("LES_AIDES_API_KEY", "")

# --- TrackDéchets ---
TRACKDECHETS_URL = "https://api.trackdechets.beta.gouv.fr/"


# ── Helpers — entreprises ──────────────────────────────────────────────────────

def _normalize_naf(code_naf: str) -> str:
    code = code_naf.strip().upper()
    if len(code) == 5 and code[:4].isdigit() and code[4].isalpha():
        return f"{code[:2]}.{code[2:]}"
    return code


def _format_entreprise(org: dict) -> dict:
    siege = org.get("siege", {}) or {}
    matching = org.get("matching_etablissements", [])
    best_etab = matching[0] if matching else siege
    return {
        "siren": org.get("siren"),
        "nom": org.get("nom_complet") or org.get("nom_raison_sociale"),
        "sigle": org.get("sigle"),
        "forme_juridique": org.get("nature_juridique_label"),
        "date_creation": org.get("date_creation"),
        "tranche_effectifs": org.get("tranche_effectif_salarie_label"),
        "code_naf": org.get("activite_principale"),
        "libelle_naf": org.get("libelle_activite_principale_entreprise"),
        "categorie": org.get("categorie_entreprise"),
        "etat": "active" if org.get("etat_administratif") == "A" else "fermée",
        "siege": {
            "siret": siege.get("siret"),
            "adresse": siege.get("adresse"),
            "code_postal": siege.get("code_postal"),
            "ville": siege.get("libelle_commune"),
            "departement": siege.get("departement"),
            "latitude": siege.get("latitude"),
            "longitude": siege.get("longitude"),
        },
        "etablissement_trouve": {
            "siret": best_etab.get("siret"),
            "adresse": best_etab.get("adresse"),
            "ville": best_etab.get("libelle_commune"),
            "code_postal": best_etab.get("code_postal"),
        } if best_etab and best_etab != siege else None,
        "dirigeants": [
            {
                "nom": f"{d.get('prenoms', '')} {d.get('nom', '')}".strip(),
                "qualite": d.get("qualite"),
                "date_naissance": d.get("date_naissance_dirigeant_partiel"),
            }
            for d in (org.get("dirigeants") or [])
        ],
        "finances": {
            "chiffre_affaires": org.get("chiffre_affaires"),
            "resultat": org.get("resultat"),
            "annee": org.get("annee_finances"),
        } if org.get("chiffre_affaires") else None,
    }


def _bounded_limit(value: int, default: int, maximum: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def _compact_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value not in (None, "")}


def _format_geoplateforme_feature(feature: dict) -> dict:
    geometry = feature.get("geometry") or {}
    properties = feature.get("properties") or {}
    coordinates = geometry.get("coordinates") or [None, None]
    longitude = coordinates[0] if len(coordinates) > 0 else None
    latitude = coordinates[1] if len(coordinates) > 1 else None

    return {
        "source": "geoplateforme",
        "id": properties.get("id") or properties.get("banId"),
        "label": properties.get("label") or properties.get("name"),
        "type": properties.get("type"),
        "score": properties.get("score"),
        "latitude": latitude,
        "longitude": longitude,
        "distance_m": properties.get("distance"),
        "adresse": {
            "numero": properties.get("housenumber"),
            "rue": properties.get("street"),
            "code_postal": properties.get("postcode"),
            "ville": properties.get("city"),
            "code_insee": properties.get("citycode"),
            "arrondissement": properties.get("district"),
            "contexte": properties.get("context"),
            "departement": properties.get("depcode"),
        },
        "index": properties.get("_type"),
        "raw_properties": properties,
    }


def _format_nominatim_place(place: dict) -> dict:
    address = place.get("address") or {}
    return {
        "source": "nominatim",
        "id": place.get("place_id") or place.get("osm_id"),
        "label": place.get("display_name") or place.get("name"),
        "type": place.get("type"),
        "class": place.get("class"),
        "score": place.get("importance"),
        "latitude": float(place["lat"]) if place.get("lat") else None,
        "longitude": float(place["lon"]) if place.get("lon") else None,
        "adresse": address,
        "raw_properties": place,
    }


def _nominatim_config_error() -> dict:
    return {
        "error": "Global geocoding is not configured.",
        "details": (
            "Set NOMINATIM_BASE_URL and NOMINATIM_USER_AGENT to a self-hosted "
            "or deliberately approved Nominatim-compatible service."
        ),
    }


# ── Helpers — Aides-Territoires ────────────────────────────────────────────────

_AT_BEARER: str = ""
_AT_BEARER_EXPIRES: float = 0.0


async def _get_at_bearer() -> str:
    global _AT_BEARER, _AT_BEARER_EXPIRES
    if _AT_BEARER and time.time() < _AT_BEARER_EXPIRES:
        return _AT_BEARER
    if not AIDES_TERRITOIRES_TOKEN:
        return ""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{AT_BASE_URL}/connexion/",
            headers={"X-AUTH-TOKEN": AIDES_TERRITOIRES_TOKEN},
        )
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, str):
        bearer = data
    else:
        bearer = (
            data.get("token") or data.get("access") or
            data.get("bearer_token") or data.get("access_token") or ""
        )
    _AT_BEARER = bearer
    _AT_BEARER_EXPIRES = time.time() + 23 * 3600  # renew 1h before expiry
    return bearer


def _name_list(items) -> list:
    if not items:
        return []
    return [i.get("name") if isinstance(i, dict) else i for i in items]


def _format_aide_at(aid) -> dict:
    if not isinstance(aid, dict):
        return {"raw": aid}
    return {
        "id": aid.get("id"),
        "slug": aid.get("slug"),
        "url": aid.get("url"),
        "name": aid.get("name"),
        "description": aid.get("description"),
        "eligibility": aid.get("eligibility"),
        "perimeter": aid.get("perimeter"),
        "perimeter_scale": aid.get("perimeter_scale"),
        "region": aid.get("region"),
        "aid_types": aid.get("aid_types") or [],
        "audiences": aid.get("targeted_audiences") or [],
        "financers": _name_list(aid.get("financers")),
        "instructors": _name_list(aid.get("instructors")),
        "programs": _name_list(aid.get("programs")),
        "categories": _name_list(aid.get("categories")),
        "subvention_rate_min": aid.get("subvention_rate_lower_bound"),
        "subvention_rate_max": aid.get("subvention_rate_upper_bound"),
        "loan_amount": aid.get("loan_amount"),
        "submission_deadline": aid.get("submission_deadline"),
        "start_date": aid.get("start_date"),
        "recurrence": aid.get("recurrence"),
        "origin_url": aid.get("origin_url"),
        "application_url": aid.get("application_url"),
        "is_call_for_project": aid.get("is_call_for_project"),
        "european_aid": aid.get("european_aid"),
        "is_live": aid.get("is_live"),
    }


# ── Helpers — les-aides.fr ─────────────────────────────────────────────────────

def _les_aides_headers() -> dict:
    return {"X-IDC": LES_AIDES_API_KEY}


# ── Helpers — TrackDéchets ─────────────────────────────────────────────────────

async def _trackdechets_gql(query: str, variables: dict | None = None) -> dict:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = {k: v for k, v in variables.items() if v is not None}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            TRACKDECHETS_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    if "errors" in data and data["errors"]:
        msg = data["errors"][0].get("message", str(data["errors"]))
        return {"error": msg}
    return data.get("data", {})


def _format_company_td(data: dict) -> dict:
    installation = data.get("installation") or {}
    return {
        "orgId": data.get("orgId"),
        "siret": data.get("siret"),
        "vatNumber": data.get("vatNumber"),
        "name": data.get("name"),
        "address": data.get("address"),
        "naf": data.get("naf"),
        "libelleNaf": data.get("libelleNaf"),
        "etatAdministratif": data.get("etatAdministratif"),
        "isRegistered": data.get("isRegistered"),
        "isDormant": data.get("isDormant"),
        "companyTypes": data.get("companyTypes", []),
        "collectorTypes": data.get("collectorTypes") or [],
        "wasteProcessorTypes": data.get("wasteProcessorTypes") or [],
        "ecoOrganismeAgreements": data.get("ecoOrganismeAgreements") or [],
        "transporterReceipt": data.get("transporterReceipt"),
        "traderReceipt": data.get("traderReceipt"),
        "brokerReceipt": data.get("brokerReceipt"),
        "vhuAgrementDemolisseur": data.get("vhuAgrementDemolisseur"),
        "vhuAgrementBroyeur": data.get("vhuAgrementBroyeur"),
        "installation": {
            "codeS3ic": installation.get("codeS3ic"),
            "urlFiche": installation.get("urlFiche"),
            "rubriques": installation.get("rubriques") or [],
            "declarations": installation.get("declarations") or [],
        } if installation else None,
        "workerCertification": data.get("workerCertification"),
    }


# ── Tools — Entreprises ────────────────────────────────────────────────────────

@mcp.tool()
async def search_entreprise(
    nom: str,
    ville: Optional[str] = None,
    code_postal: Optional[str] = None,
    code_naf: Optional[str] = None,
    limite: int = 5,
) -> dict:
    """
    Search French companies by name, optionally filtered by city/postal code/NAF.

    Args:
        nom: Company name (e.g. "GAINERIE 91", "couture Cholet")
        ville: City name (e.g. "Montgeron")
        code_postal: Postal code (e.g. "91230")
        code_naf: NAF/APE code (e.g. "1413Z")
        limite: Max results 1-25, default 5
    """
    query = f"{nom} {ville}".strip() if ville else nom
    params = {
        "q": query, "page": 1, "per_page": min(limite, 25),
        "minimal": "true",
        "include": "siege,matching_etablissements,dirigeants,finances",
    }
    if code_postal: params["code_postal"] = code_postal
    if code_naf: params["activite_principale"] = _normalize_naf(code_naf)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{ENTREPRISES_BASE_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    return {
        "total_found": data.get("total_results", 0),
        "returned": len(results),
        "query": {"nom": nom, "ville": ville, "code_postal": code_postal},
        "entreprises": [_format_entreprise(r) for r in results],
    }


@mcp.tool()
async def get_entreprise_by_siren(siren: str) -> dict:
    """
    Get full details for a French company by SIREN number (9 digits).

    Args:
        siren: 9-digit SIREN (e.g. "552032534")
    """
    siren_clean = siren.replace(" ", "").replace("-", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ENTREPRISES_BASE_URL}/search",
            params={"q": siren_clean, "page": 1, "per_page": 1,
                    "minimal": "true",
                    "include": "siege,matching_etablissements,dirigeants,finances"},
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    if not results:
        return {"error": f"No company found for SIREN {siren}"}
    return _format_entreprise(results[0])


@mcp.tool()
async def enrich_batch(entreprises: list[dict]) -> dict:
    """
    Enrich up to 20 companies from [{nom, ville?, code_postal?}].
    Returns best match per entry, or found=false if not found.

    Args:
        entreprises: e.g. [{"nom": "GAINERIE 91", "code_postal": "91230"}, ...]
                     Maximum 20 entries per call.
    """
    if len(entreprises) > 20:
        return {"error": "Maximum 20 companies per call."}

    enriched = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for entry in entreprises:
            nom = entry.get("nom", "")
            cp = entry.get("code_postal")
            ville = entry.get("ville")
            query = f"{nom} {ville}".strip() if ville else nom
            params = {"q": query, "page": 1, "per_page": 1,
                      "minimal": "true",
                      "include": "siege,matching_etablissements,dirigeants,finances"}
            if cp: params["code_postal"] = cp
            try:
                resp = await client.get(f"{ENTREPRISES_BASE_URL}/search", params=params)
                resp.raise_for_status()
                results = resp.json().get("results", [])
                if results:
                    enriched.append({"input": entry, "found": True,
                                     "data": _format_entreprise(results[0])})
                else:
                    enriched.append({"input": entry, "found": False, "data": None})
            except Exception as e:
                enriched.append({"input": entry, "found": False,
                                 "error": str(e), "data": None})

    found_count = sum(1 for e in enriched if e["found"])
    return {"total": len(enriched), "found": found_count,
            "not_found": len(enriched) - found_count, "results": enriched}


# ── Tools — Géocodage ──────────────────────────────────────────────────────────

@mcp.tool()
async def geocode_address(
    adresse: str,
    ville: Optional[str] = None,
    code_postal: Optional[str] = None,
    code_insee: Optional[str] = None,
    departement: Optional[str] = None,
    pays: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    index: str = "address",
    type_resultat: Optional[str] = None,
    limite: int = 5,
    source: str = "france",
) -> dict:
    """
    Geocode an address/place/parcel and return latitude/longitude candidates.

    Args:
        adresse: Address or place to geocode (e.g. "10 avenue des Champs Elysees")
        ville: Optional city filter or query hint
        code_postal: Optional postal code filter
        code_insee: Optional INSEE city code filter
        departement: Optional department code filter
        pays: Optional country hint; only used by the global source
        latitude: Optional latitude used to bias ranking
        longitude: Optional longitude used to bias ranking
        index: Geoplateforme indexes: address, poi, parcel, or comma-separated
        type_resultat: Optional address type: housenumber, street, locality, municipality
        limite: Max results 1-50, default 5
        source: "france" for Geoplateforme, "global" for configured Nominatim-compatible
    """
    limit = _bounded_limit(limite, 5, MAX_GEOCODING_RESULTS)
    source_key = source.strip().lower()

    if source_key in ("france", "geoplateforme", "ban"):
        query = " ".join(part for part in (adresse, ville) if part)
        params = _compact_params({
            "q": query,
            "limit": limit,
            "autocomplete": "0",
            "index": index,
            "postcode": code_postal,
            "citycode": code_insee,
            "depcode": departement,
            "city": ville,
            "type": type_resultat,
            "lat": latitude,
            "lon": longitude,
        })

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{FR_GEOCODING_BASE_URL}/search", params=params)
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features", [])
        return {
            "source": "geoplateforme",
            "query": {
                "adresse": adresse,
                "ville": ville,
                "code_postal": code_postal,
                "code_insee": code_insee,
                "departement": departement,
                "index": index,
            },
            "returned": len(features),
            "results": [_format_geoplateforme_feature(feature) for feature in features],
        }

    if source_key in ("global", "world", "nominatim"):
        if not NOMINATIM_BASE_URL or not NOMINATIM_USER_AGENT:
            return _nominatim_config_error()

        query = " ".join(part for part in (adresse, ville, pays) if part)
        params = _compact_params({
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": limit,
            "countrycodes": pays.lower() if pays and len(pays) <= 3 else None,
        })
        headers = {"User-Agent": NOMINATIM_USER_AGENT}

        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(f"{NOMINATIM_BASE_URL}/search", params=params)
            resp.raise_for_status()
            places = resp.json()

        return {
            "source": "nominatim",
            "query": {"adresse": adresse, "ville": ville, "pays": pays},
            "returned": len(places),
            "results": [_format_nominatim_place(place) for place in places],
        }

    return {"error": f"Unknown geocoding source: {source}"}


@mcp.tool()
async def reverse_geocode(
    latitude: float,
    longitude: float,
    code_postal: Optional[str] = None,
    code_insee: Optional[str] = None,
    index: str = "address",
    type_resultat: Optional[str] = None,
    limite: int = 5,
    source: str = "france",
) -> dict:
    """
    Reverse geocode coordinates and return nearest address/place/parcel candidates.

    Args:
        latitude: Latitude in WGS84
        longitude: Longitude in WGS84
        code_postal: Optional postal code filter
        code_insee: Optional INSEE city code filter
        index: Geoplateforme indexes: address, poi, parcel, or comma-separated
        type_resultat: Optional address type: housenumber, street, locality, municipality
        limite: Max results 1-50, default 5
        source: "france" for Geoplateforme, "global" for configured Nominatim-compatible
    """
    limit = _bounded_limit(limite, 5, MAX_GEOCODING_RESULTS)
    source_key = source.strip().lower()

    if source_key in ("france", "geoplateforme", "ban"):
        params = _compact_params({
            "lat": latitude,
            "lon": longitude,
            "limit": limit,
            "index": index,
            "postcode": code_postal,
            "citycode": code_insee,
            "type": type_resultat,
        })

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{FR_GEOCODING_BASE_URL}/reverse", params=params)
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features", [])
        return {
            "source": "geoplateforme",
            "query": {
                "latitude": latitude,
                "longitude": longitude,
                "code_postal": code_postal,
                "code_insee": code_insee,
                "index": index,
            },
            "returned": len(features),
            "results": [_format_geoplateforme_feature(feature) for feature in features],
        }

    if source_key in ("global", "world", "nominatim"):
        if not NOMINATIM_BASE_URL or not NOMINATIM_USER_AGENT:
            return _nominatim_config_error()

        params = {
            "lat": latitude,
            "lon": longitude,
            "format": "jsonv2",
            "addressdetails": 1,
        }
        headers = {"User-Agent": NOMINATIM_USER_AGENT}

        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            resp = await client.get(f"{NOMINATIM_BASE_URL}/reverse", params=params)
            resp.raise_for_status()
            place = resp.json()

        return {
            "source": "nominatim",
            "query": {"latitude": latitude, "longitude": longitude},
            "returned": 1 if place else 0,
            "results": [_format_nominatim_place(place)] if place else [],
        }

    return {"error": f"Unknown geocoding source: {source}"}


# ── Tools — Aides-Territoires ──────────────────────────────────────────────────

@mcp.tool()
async def list_perimetres_at(
    q: str,
    scale: Optional[str] = None,
    limite: int = 10,
) -> dict:
    """
    Resolve a geographic name to a perimeter code for use in search_aides_territoires.

    Args:
        q: Name to search (e.g. "Bretagne", "Finistère", "Brest")
        scale: Optional scale filter: region, department, epci, commune, overseas
        limite: Max results 1-20, default 10
    """
    limit = _bounded_limit(limite, 10, 20)
    params: list[tuple[str, Any]] = [("q", q), ("itemsPerPage", limit)]
    if scale:
        params.append(("scale", scale))

    bearer = await _get_at_bearer()
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        resp = await client.get(f"{AT_BASE_URL}/perimeters/", params=params)
        resp.raise_for_status()
        data = resp.json()

    if isinstance(data, list):
        members = data
    elif isinstance(data, dict):
        members = data.get("hydra:member") or data.get("results") or []
    else:
        members = []
    members = members[:limit]
    return {
        "total": data.get("hydra:totalItems", len(members)) if isinstance(data, dict) else len(members),
        "returned": len(members),
        "perimetres": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "code": p.get("code"),
                "scale": p.get("scale"),
                "scale_name": p.get("scale_name"),
                "insee": p.get("insee"),
                "siren": p.get("siren"),
            }
            for p in members
        ],
    }


@mcp.tool()
async def search_aides_territoires(
    keyword: Optional[str] = None,
    perimeter_code: Optional[str] = None,
    audience_slug: Optional[str] = None,
    aid_type_slug: Optional[str] = None,
    theme_slug: Optional[str] = None,
    limite: int = 10,
) -> dict:
    """
    Search public aids for collectivités and porteurs de projets (Aides-Territoires).

    Args:
        keyword: Free text search
        perimeter_code: INSEE or SIREN code of the territory (get it from list_perimetres_at)
        audience_slug: Beneficiary type slug, e.g. commune, epci, department, region,
                       association, private-person, company, farmer
        aid_type_slug: Aid type slug, e.g. grant, loan, recoverable-advance,
                       technical-assistance, financial, other
        theme_slug: Theme/category slug, e.g. biodiversity, energy, mobility, housing,
                    digital, culture, sport, water, waste, urban-planning
        limite: Max results 1-50, default 10
    """
    limit = _bounded_limit(limite, 10, 50)
    params: list[tuple[str, Any]] = [("itemsPerPage", limit)]
    if keyword:
        params.append(("keyword", keyword))
    if perimeter_code:
        params.append(("perimeter_codes", perimeter_code))
    if audience_slug:
        params.append(("organization_type_slugs", audience_slug))
    if aid_type_slug:
        params.append(("aid_type_slugs", aid_type_slug))
    if theme_slug:
        params.append(("category_slugs", theme_slug))

    bearer = await _get_at_bearer()
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(f"{AT_BASE_URL}/aids/", params=params)
        resp.raise_for_status()
        data = resp.json()

    if isinstance(data, list):
        members = data
    elif isinstance(data, dict):
        members = data.get("hydra:member") or data.get("results") or []
    else:
        members = []
    members = members[:limit]
    return {
        "total": data.get("hydra:totalItems", len(members)) if isinstance(data, dict) else len(members),
        "returned": len(members),
        "query": {
            "keyword": keyword,
            "perimeter_code": perimeter_code,
            "audience_slug": audience_slug,
            "aid_type_slug": aid_type_slug,
            "theme_slug": theme_slug,
        },
        "aides": [_format_aide_at(a) for a in members],
    }


# ── Tools — les-aides.fr ───────────────────────────────────────────────────────

@mcp.tool()
async def list_refs_les_aides(type: str) -> dict:
    """
    List valid reference values for les-aides.fr search filters.

    Args:
        type: One of: domaines, filieres, regions, departements, moyens
              - domaines: aid category IDs (use in search_aides_les_aides domaine param)
              - filieres: business sector slugs (use in filiere param)
              - regions: region names (use in region param)
              - departements: department codes (use in departement param)
              - moyens: intervention method IDs (use in moyen param)
    """
    valid = {"domaines", "filieres", "regions", "departements", "moyens"}
    if type not in valid:
        return {"error": f"Invalid type '{type}'. Must be one of: {', '.join(sorted(valid))}"}
    if not LES_AIDES_API_KEY:
        return {"error": "LES_AIDES_API_KEY environment variable is not set."}

    async with httpx.AsyncClient(timeout=10.0, headers=_les_aides_headers()) as client:
        resp = await client.get(f"{LES_AIDES_BASE_URL}/liste/{type}")
        resp.raise_for_status()
        data = resp.json()

    return {"type": type, "count": len(data) if isinstance(data, list) else None, "values": data}


@mcp.tool()
async def search_aides_les_aides(
    siren: Optional[str] = None,
    ape: Optional[str] = None,
    region: Optional[str] = None,
    departement: Optional[str] = None,
    domaine: Optional[int] = None,
    filiere: Optional[str] = None,
    moyen: Optional[int] = None,
) -> dict:
    """
    Search enterprise aids (national + European) via les-aides.fr (CCI France database).
    Use list_refs_les_aides to get valid values for domaine, filiere, region, departement.

    Args:
        siren: Company SIREN number for personalized results
        ape: NAF/APE code of the company activity (e.g. "6201Z")
        region: Region name (e.g. "Bretagne", "Ile-de-France")
        departement: Department code (e.g. "29", "75")
        domaine: Aid domain/category ID (integer, from list_refs_les_aides domaines)
        filiere: Business sector slug (from list_refs_les_aides filieres)
        moyen: Intervention method ID (integer, from list_refs_les_aides moyens)
    """
    if not LES_AIDES_API_KEY:
        return {"error": "LES_AIDES_API_KEY environment variable is not set."}

    params: list[tuple[str, Any]] = []
    if siren:
        params.append(("siren", siren))
    if ape:
        params.append(("ape", ape))
    if region:
        params.append(("region", region))
    if departement:
        params.append(("departement", departement))
    if domaine is not None:
        params.append(("domaine", domaine))
    if filiere:
        params.append(("filiere", filiere))
    if moyen is not None:
        params.append(("moyen", moyen))

    async with httpx.AsyncClient(timeout=15.0, headers=_les_aides_headers()) as client:
        resp = await client.get(f"{LES_AIDES_BASE_URL}/aides", params=params)
        if resp.status_code == 403:
            return {
                "error": "403 Forbidden — les-aides.fr requires at least 3 combined filters for this query. "
                         "Try adding a domaine (use list_refs_les_aides to get valid IDs) "
                         "or a filiere alongside region/ape.",
                "query": _compact_params({
                    "siren": siren, "ape": ape, "region": region,
                    "departement": departement, "domaine": domaine,
                    "filiere": filiere, "moyen": moyen,
                }),
            }
        resp.raise_for_status()
        data = resp.json()

    aides = data if isinstance(data, list) else data.get("aides") or data.get("results") or []
    return {
        "returned": len(aides),
        "query": _compact_params({
            "siren": siren, "ape": ape, "region": region,
            "departement": departement, "domaine": domaine,
            "filiere": filiere, "moyen": moyen,
        }),
        "aides": aides,
    }


@mcp.tool()
async def get_aide_les_aides(dispositif: int, requete: Optional[int] = None) -> dict:
    """
    Get full details of an enterprise aid from les-aides.fr.

    Args:
        dispositif: Aid program ID (from search_aides_les_aides results)
        requete: Optional request context ID (from search_aides_les_aides results, if present)
    """
    if not LES_AIDES_API_KEY:
        return {"error": "LES_AIDES_API_KEY environment variable is not set."}

    params: list[tuple[str, Any]] = [("dispositif", dispositif)]
    if requete is not None:
        params.append(("requete", requete))

    async with httpx.AsyncClient(timeout=10.0, headers=_les_aides_headers()) as client:
        resp = await client.get(f"{LES_AIDES_BASE_URL}/aide", params=params)
        resp.raise_for_status()
        data = resp.json()

    return data if isinstance(data, dict) else {"aide": data}


# ── Tools — TrackDéchets ───────────────────────────────────────────────────────

_TD_COMPANY_QUERY = """
query CompanyInfos($siret: String, $clue: String) {
  companyInfos(siret: $siret, clue: $clue) {
    orgId siret vatNumber name address naf libelleNaf
    etatAdministratif isRegistered isDormant
    companyTypes collectorTypes wasteProcessorTypes
    ecoOrganismeAgreements
    transporterReceipt { receiptNumber validityLimit department }
    traderReceipt { receiptNumber validityLimit department }
    brokerReceipt { receiptNumber validityLimit department }
    vhuAgrementDemolisseur { agrementNumber department }
    vhuAgrementBroyeur { agrementNumber department }
    installation {
      codeS3ic urlFiche
      rubriques { rubrique alinea etatActivite regimeAutorise activite category volume unite }
      declarations { annee codeDechet libDechet gerepType }
    }
    workerCertification {
      hasSubSectionFour hasSubSectionThree certificationNumber validityLimit organisation
    }
  }
}
"""

_TD_ECO_QUERY = """
query EcoOrganismes($handleBsdd: Boolean, $handleBsda: Boolean, $handleBsdasri: Boolean, $handleBsvhu: Boolean) {
  ecoOrganismes(handleBsdd: $handleBsdd, handleBsda: $handleBsda, handleBsdasri: $handleBsdasri, handleBsvhu: $handleBsvhu) {
    id name siret address handleBsdd handleBsda handleBsdasri handleBsvhu
  }
}
"""


@mcp.tool()
async def trackdechets_company(
    siret: Optional[str] = None,
    clue: Optional[str] = None,
) -> dict:
    """
    Get a company's waste regulatory profile from TrackDéchets (public data, no auth needed).
    Returns ICPE classifications, GEREP declarations, transporter/trader/broker receipts,
    VHU agreements, waste operator types, and TrackDéchets registration status.

    Args:
        siret: 14-digit SIRET of the establishment
        clue: Intra-community VAT number (alternative to SIRET)
    """
    if not siret and not clue:
        return {"error": "Provide either siret or clue."}

    data = await _trackdechets_gql(
        _TD_COMPANY_QUERY,
        {"siret": siret, "clue": clue},
    )
    if "error" in data:
        return data
    company = data.get("companyInfos")
    if not company:
        return {"error": "No company found."}
    return _format_company_td(company)


@mcp.tool()
async def trackdechets_eco_organismes(
    handle_bsdd: Optional[bool] = None,
    handle_bsda: Optional[bool] = None,
    handle_bsdasri: Optional[bool] = None,
    handle_bsvhu: Optional[bool] = None,
) -> dict:
    """
    List eco-organisms registered on TrackDéchets (public data, no auth needed).
    Eco-organisms manage the end-of-life of products under extended producer responsibility.

    Args:
        handle_bsdd: Filter to eco-organisms handling dangerous waste (BSDD)
        handle_bsda: Filter to eco-organisms handling asbestos waste (BSDA)
        handle_bsdasri: Filter to eco-organisms handling healthcare waste (BSDASRI)
        handle_bsvhu: Filter to eco-organisms handling end-of-life vehicles (BSVHU)
    """
    data = await _trackdechets_gql(
        _TD_ECO_QUERY,
        {
            "handleBsdd": handle_bsdd,
            "handleBsda": handle_bsda,
            "handleBsdasri": handle_bsdasri,
            "handleBsvhu": handle_bsvhu,
        },
    )
    if "error" in data:
        return data
    orgs = data.get("ecoOrganismes", [])
    return {"count": len(orgs), "eco_organismes": orgs}


# ── ASGI app ───────────────────────────────────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=_transport_security(),
)

# Claude et ChatGPT appellent normalement le MCP cote serveur. Ce middleware
# permet aussi les clients navigateur et MCP Inspector ; la validation Origin
# effective reste assuree par TransportSecuritySettings sur la requete MCP.
app = CORSMiddleware(
    mcp_app,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
