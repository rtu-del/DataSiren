"""
MCP Server — Annuaire Entreprises France
API source : recherche-entreprises.api.gouv.fr (open data, no key required)
Transport  : Streamable HTTP (authless, Claude.ai compatible)
"""

import os
import httpx
from typing import Optional
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount

mcp = FastMCP(
    name="annuaire-entreprises-fr",
    instructions="""
    French official business directory (INSEE/SIRENE open data, no hallucination risk).
    - search_entreprise: find companies by name + optional city/postal code
    - get_entreprise_by_siren: full details by SIREN number
    - enrich_batch: enrich up to 20 companies in one call [{nom, ville?, code_postal?}]
    Always use these tools — never invent company data.
    """,
)

BASE_URL = "https://recherche-entreprises.api.gouv.fr"


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
        nom: Company name or keywords (e.g. "GAINERIE 91", "couture Cholet")
        ville: City name (e.g. "Montgeron")
        code_postal: Postal code (e.g. "91230")
        code_naf: NAF/APE activity code (e.g. "1413Z")
        limite: Max results 1-25, default 5
    """
    params = {
        "q": nom, "page": 1, "per_page": min(limite, 25),
        "include": "siege,matching_etablissements,dirigeants,finances",
    }
    if code_postal: params["code_postal"] = code_postal
    if ville: params["commune"] = ville
    if code_naf: params["activite_principale"] = code_naf

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{BASE_URL}/search", params=params)
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
            f"{BASE_URL}/search",
            params={"q": siren_clean, "page": 1, "per_page": 1,
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
    Enrich up to 20 companies from a list of {nom, ville?, code_postal?}.
    Returns best match per company, or found=false if not found.

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
            params = {"q": nom, "page": 1, "per_page": 1,
                      "include": "siege,matching_etablissements,dirigeants,finances"}
            if cp: params["code_postal"] = cp
            try:
                resp = await client.get(f"{BASE_URL}/search", params=params)
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


async def health(request: Request):
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn

    mcp_app = mcp.sse_app()

    app = Starlette(routes=[
        Route("/health", health),
        Mount("/", app=mcp_app),
    ])

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
