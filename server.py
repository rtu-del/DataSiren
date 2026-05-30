"""
MCP Server — Annuaire Entreprises France
OAuth2 Authorization Code + PKCE (Claude.ai compatible)
"""

import os
import hashlib
import base64
import secrets
import httpx
from typing import Optional
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, HTMLResponse
from starlette.routing import Route, Mount
from starlette.middleware.base import BaseHTTPMiddleware

# ── Config ─────────────────────────────────────────────────────────────────────
CLIENT_ID     = os.environ.get("MCP_CLIENT_ID", "datasiren")
CLIENT_SECRET = os.environ.get("MCP_CLIENT_SECRET", "")

# In-memory stores (fine for single-instance Railway deployment)
auth_codes = {}   # code -> {code_challenge, redirect_uri}
tokens     = {}   # access_token -> True

# ── MCP ────────────────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="annuaire-entreprises-fr",
    instructions="""
    French official business directory (INSEE/SIRENE open data).
    - search_entreprise: find by name + optional city/postal code
    - get_entreprise_by_siren: full details by SIREN
    - enrich_batch: enrich up to 20 companies in one call
    Never hallucinate company data — always use these tools.
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
async def search_entreprise(nom: str, ville: Optional[str] = None,
                             code_postal: Optional[str] = None,
                             code_naf: Optional[str] = None, limite: int = 5) -> dict:
    """Search French companies by name + optional city/postal/NAF."""
    params = {"q": nom, "page": 1, "per_page": min(limite, 25),
               "include": "siege,matching_etablissements,dirigeants,finances"}
    if code_postal: params["code_postal"] = code_postal
    if ville: params["commune"] = ville
    if code_naf: params["activite_principale"] = code_naf
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{BASE_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    return {"total_found": data.get("total_results", 0), "returned": len(results),
            "entreprises": [_format_entreprise(r) for r in results]}


@mcp.tool()
async def get_entreprise_by_siren(siren: str) -> dict:
    """Get full details for a French company by SIREN (9 digits)."""
    siren_clean = siren.replace(" ", "").replace("-", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{BASE_URL}/search",
            params={"q": siren_clean, "page": 1, "per_page": 1,
                    "include": "siege,matching_etablissements,dirigeants,finances"})
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    if not results:
        return {"error": f"No company found for SIREN {siren}"}
    return _format_entreprise(results[0])


@mcp.tool()
async def enrich_batch(entreprises: list[dict]) -> dict:
    """Enrich up to 20 companies from [{nom, ville?, code_postal?}] list."""
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
                    enriched.append({"input": entry, "found": True, "data": _format_entreprise(results[0])})
                else:
                    enriched.append({"input": entry, "found": False, "data": None})
            except Exception as e:
                enriched.append({"input": entry, "found": False, "error": str(e), "data": None})
    found_count = sum(1 for e in enriched if e["found"])
    return {"total": len(enriched), "found": found_count,
            "not_found": len(enriched) - found_count, "results": enriched}


# ── Auth middleware ────────────────────────────────────────────────────────────
BYPASS_PATHS = {"/health", "/oauth/token", "/oauth/authorize",
                "/.well-known/oauth-authorization-server"}

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in BYPASS_PATHS or not CLIENT_SECRET:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip()
        if token not in tokens:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ── OAuth2 endpoints ───────────────────────────────────────────────────────────
async def oauth_discovery(request: Request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "response_types_supported": ["code"],
    })


async def oauth_authorize(request: Request):
    """
    Authorization endpoint — shows a simple approval page.
    Claude.ai redirects the user here with PKCE params.
    """
    params = dict(request.query_params)
    redirect_uri    = params.get("redirect_uri", "")
    code_challenge  = params.get("code_challenge", "")
    state           = params.get("state", "")
    client_id_param = params.get("client_id", "")

    if client_id_param != CLIENT_ID:
        return HTMLResponse("<h1>Invalid client_id</h1>", status_code=400)

    # Store challenge + redirect for token exchange
    code = secrets.token_urlsafe(32)
    auth_codes[code] = {"code_challenge": code_challenge, "redirect_uri": redirect_uri}

    # Auto-approve page — one click to authorize
    html = f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><title>DataSiren — Autorisation</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; background: #f5f5f5; }}
  .card {{ background: white; padding: 2rem 2.5rem; border-radius: 12px;
           box-shadow: 0 2px 16px rgba(0,0,0,.1); text-align: center; max-width: 380px; }}
  h1 {{ font-size: 1.3rem; margin-bottom: .5rem; }}
  p {{ color: #555; margin-bottom: 1.5rem; font-size: .95rem; }}
  a.btn {{ display: inline-block; background: #185FA5; color: white; padding: .7rem 2rem;
           border-radius: 8px; text-decoration: none; font-weight: 600; }}
  a.btn:hover {{ background: #134d8a; }}
</style>
</head>
<body>
  <div class="card">
    <h1>🔍 DataSiren</h1>
    <p>Autoriser Claude à accéder à l'annuaire officiel des entreprises françaises (données gouvernementales open data).</p>
    <a class="btn" href="/oauth/approve?code={code}&redirect_uri={redirect_uri}&state={state}">
      Autoriser l'accès
    </a>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


async def oauth_approve(request: Request):
    """User clicked approve — redirect back to Claude with the auth code."""
    params = dict(request.query_params)
    code         = params.get("code", "")
    redirect_uri = params.get("redirect_uri", "")
    state        = params.get("state", "")

    if code not in auth_codes:
        return HTMLResponse("<h1>Code invalide ou expiré</h1>", status_code=400)

    callback = f"{redirect_uri}?code={code}&state={state}"
    return RedirectResponse(callback)


async def oauth_token(request: Request):
    """Token endpoint — exchanges auth code for Bearer token (PKCE verified)."""
    try:
        body = await request.form()
        body = dict(body)
    except Exception:
        body = await request.json()

    grant_type    = body.get("grant_type", "")
    code          = body.get("code", "")
    code_verifier = body.get("code_verifier", "")
    redirect_uri  = body.get("redirect_uri", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    stored = auth_codes.get(code)
    if not stored:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    # Verify PKCE: SHA256(code_verifier) base64url == code_challenge
    digest = hashlib.sha256(code_verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    if computed != stored["code_challenge"]:
        return JSONResponse({"error": "invalid_grant", "detail": "PKCE mismatch"}, status_code=400)

    # Issue token
    del auth_codes[code]
    access_token = secrets.token_urlsafe(32)
    tokens[access_token] = True

    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 86400,
    })


async def health(request: Request):
    return JSONResponse({"status": "ok"})


# ── App assembly ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    mcp_app = mcp.sse_app()

    app = Starlette(routes=[
        Route("/health", health),
        Route("/.well-known/oauth-authorization-server", oauth_discovery),
        Route("/oauth/authorize", oauth_authorize),
        Route("/oauth/approve", oauth_approve),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Mount("/", app=mcp_app),
    ])
    app.add_middleware(BearerAuthMiddleware)

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
