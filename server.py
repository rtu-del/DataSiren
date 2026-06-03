"""
MCP Server — Donnees ouvertes France
Transport : Streamable HTTP stateless (authless, Claude.ai compatible)
v6
"""

import os
import httpx
import uvicorn
from typing import Any, Optional
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    name="annuaire-entreprises-fr",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
    instructions="""
    French official open data tools for companies and geocoding.
    - search_entreprise: find companies by name + optional city/postal code
    - get_entreprise_by_siren: full details by SIREN number
    - enrich_batch: enrich up to 20 companies in one call
    - geocode_address: get coordinates from an address/place/parcel
    - reverse_geocode: get the nearest address/place/parcel from coordinates
    Never hallucinate company or address data — always use these tools.
    """,
)
mcp_app = mcp.streamable_http_app()

ENTREPRISES_BASE_URL = "https://recherche-entreprises.api.gouv.fr"
FR_GEOCODING_BASE_URL = os.environ.get(
    "FR_GEOCODING_BASE_URL",
    "https://data.geopf.fr/geocodage",
).rstrip("/")
NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "").rstrip("/")
NOMINATIM_USER_AGENT = os.environ.get("NOMINATIM_USER_AGENT", "")
MAX_GEOCODING_RESULTS = 50


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


async def app(scope, receive, send):
    if scope["type"] == "http" and scope.get("path") == "/health":
        body = b'{"status":"ok"}'
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
        return

    await mcp_app(scope, receive, send)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
