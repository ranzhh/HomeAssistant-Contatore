"""Login SET Distribuzione: Azure AD B2C, Authorization Code + PKCE, con la
pagina di login ospitata da B2C guidata "a mano" (nessun browser).

Flusso verificato su cattura reale del 30/09/2026 (Chrome, account
Retail_SET) - vedi documentation/protocols/set-distribuzione-protocol.md,
sezione "Login". Niente password grant (a differenza di Ireti), niente
OTP (a differenza di E-Distribuzione): sono 5 richieste in sequenza,
tutte verso {AUTH_DOMAIN}, con i cookie x-ms-cpim-* che B2C imposta al
passo 1 e rilegge ai passi 2-3 (serve una sessione aiohttp con cookie jar,
come async_create_clientsession di Home Assistant):

  1. GET  /oauth2/v2.0/authorize?client_id=...&code_challenge=...&state=...
         -> HTML della pagina di login; dentro, un blocco
            `var SETTINGS = {...}` con `csrf` (token anti-CSRF) e `transId`
            (StateProperties=..., l'id della transazione B2C), più
            `hosts.tenant` (path base della policy, con la maiuscola
            "B2C_1A_signin") e `api` ("CombinedSigninAndSignup").
  2. POST {tenant}/SelfAsserted?tx={transId}&p={policy}
         form: request_type=RESPONSE&signInName=<email>&password=<pw>
         header X-CSRF-TOKEN: <csrf>
         -> {"status": "200"} se le credenziali sono giuste.
  3. GET  {tenant}/api/CombinedSigninAndSignup/confirmed
             ?rememberMe=true&csrf_token=<csrf>&tx=<transId>&p=<policy>
         -> 302 Location: {REDIRECT_URI}/#state=...&client_info=...&code=...
            (response_mode=fragment: il code sta nel fragment dell'URL).
  4. POST /oauth2/v2.0/token
         grant_type=authorization_code&code=...&code_verifier=...
         -> access_token (1h), refresh_token (24h), id_token.

Il refresh (grant_type=refresh_token sullo stesso endpoint) non compare
nella cattura (durata < 1h) ma è verificato da terminale il 30/09/2026
(scripts/verify_set_login.py --refresh) - il coordinator lo usa
e ricade sul login completo se fallisce.

I nomi dei campi del form al passo 2 (`signInName`, `password`) vengono
dalla pagina custom della UI di login (signuporsignin-set-ui.html) e dallo
script B2C che la invia (`request_type=RESPONSE&` + id/valore dei campi);
il body reale del POST non è nella cattura (redatto) ma il login con
questi campi funziona (verificato da terminale il 30/09/2026).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import aiohttp

from .const import (
    AUTH_DOMAIN,
    AUTHORIZE_URL,
    CLIENT_ID,
    HEADERS_BROWSER,
    POLICY,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

# `var SETTINGS = {...};` nella pagina /authorize: è l'unico posto da cui
# leggere csrf/transId. Il blocco è JSON valido (B2C lo serializza così).
_RE_SETTINGS = re.compile(r"var\s+SETTINGS\s*=\s*(\{.*?\})\s*;", re.DOTALL)


class SetAuthError(Exception):
    """Errore generico di autenticazione (rete, pagina inattesa, flusso
    interrotto da qualcosa che non sappiamo gestire)."""


class SetInvalidCredentials(SetAuthError):
    """Email o password rifiutate da B2C al passo SelfAsserted."""


@dataclass
class SetTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int
    refresh_token_expires_in: int | None


def _pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) S256 - RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _estrai_settings(html: str) -> dict:
    match = _RE_SETTINGS.search(html)
    if not match:
        raise SetAuthError(
            "Pagina di login B2C inattesa: blocco SETTINGS non trovato "
            "(la pagina /authorize è cambiata?)"
        )
    try:
        return json.loads(match.group(1))
    except ValueError as err:
        raise SetAuthError(f"Blocco SETTINGS della pagina di login non parsabile: {err}") from err


def _code_da_location(location: str) -> str:
    """Estrae `code` dal redirect finale, cercando sia nel fragment
    (response_mode=fragment, osservato) sia nella query (per robustezza)."""
    parti = urlsplit(location)
    for pezzo in (parti.fragment, parti.query):
        code = parse_qs(pezzo).get("code")
        if code and code[0]:
            return code[0]
    errore = parse_qs(parti.fragment).get("error_description") or parse_qs(parti.query).get(
        "error_description"
    )
    raise SetAuthError(
        "Redirect finale B2C senza 'code'"
        + (f": {errore[0]}" if errore else f" (Location: {parti.scheme}://{parti.netloc}/...)")
    )


def crea_cookie_jar() -> aiohttp.CookieJar:
    """Il cookie jar da usare per la sessione che parla con B2C.

    OBBLIGATORIO quote_cookie=False: il jar di default di aiohttp mette tra
    virgolette i valori dei cookie con caratteri "speciali", e i cookie di
    transazione di B2C (x-ms-cpim-csrf, x-ms-cpim-trans, x-ms-cpim-cache|...)
    ne sono pieni. Rimandati tra virgolette, B2C risponde `400 Bad Request`
    (HTML) a QUALSIASI richiesta successiva - SelfAsserted, ma anche un
    secondo /authorize. Verificato dal vivo il 30/09/2026 (senza credenziali:
    basta /authorize due volte nella stessa sessione). Con async_create_clientsession
    di Home Assistant si passa come kwarg: cookie_jar=crea_cookie_jar()."""
    return aiohttp.CookieJar(quote_cookie=False)


class SetAuthClient:
    """Esegue il login B2C e ritorna i token. La sessione deve avere un
    cookie jar che NON metta tra virgolette i valori (vedi crea_cookie_jar):
    B2C lega csrf/transId ai cookie x-ms-cpim-*."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        jar = getattr(session, "cookie_jar", None)
        if getattr(jar, "_quote_cookie", False):
            # Meglio fallire subito con una spiegazione che con un "400 Bad
            # Request" nudo al secondo passo (vedi crea_cookie_jar).
            raise SetAuthError(
                "La sessione aiohttp usa un CookieJar con quote_cookie=True: B2C rifiuta "
                "i cookie tra virgolette. Crea la sessione con cookie_jar=crea_cookie_jar()."
            )

    async def async_login(self, email: str, password: str) -> SetTokens:
        """Solleva SetInvalidCredentials su credenziali errate, SetAuthError
        per qualunque altro problema."""
        try:
            return await self._login(email, password)
        except aiohttp.ClientError as err:
            raise SetAuthError(f"Errore di trasporto durante il login SET: {err}") from err

    async def _login(self, email: str, password: str) -> SetTokens:
        # Col cookie SSO di rememberMe /authorize salta il form e rimanda al portale.
        self._session.cookie_jar.clear()
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)

        # 1. pagina di login (HTML) + cookie di transazione
        params = {
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "client-request-id": str(uuid.uuid4()),
            "response_mode": "fragment",
            "response_type": "code",
            "x-client-SKU": "msal.js.browser",
            "x-client-VER": "2.39.0",
            "client_info": "1",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
            "state": state,
        }
        headers_html = {**HEADERS_BROWSER, "Accept": "text/html,application/xhtml+xml"}
        async with self._session.get(AUTHORIZE_URL, params=params, headers=headers_html) as resp:
            if resp.status != 200:
                raise SetAuthError(f"/authorize ha risposto HTTP {resp.status}")
            settings = _estrai_settings(await resp.text())

        csrf = settings.get("csrf")
        trans_id = settings.get("transId")
        hosts = settings.get("hosts") or {}
        tenant_path = hosts.get("tenant")  # es. "/<tenant>/B2C_1A_signin"
        policy = hosts.get("policy") or POLICY
        api = settings.get("api") or "CombinedSigninAndSignup"
        if not (csrf and trans_id and tenant_path):
            raise SetAuthError(
                "SETTINGS della pagina di login senza csrf/transId/hosts.tenant "
                f"(chiavi presenti: {sorted(settings)})"
            )
        base = f"https://{AUTH_DOMAIN}{tenant_path}"

        # 2. invio credenziali
        headers_xhr = {
            **HEADERS_BROWSER,
            "X-CSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": AUTHORIZE_URL,
            "Origin": f"https://{AUTH_DOMAIN}",
        }
        form = {"request_type": "RESPONSE", "signInName": email, "password": password}
        async with self._session.post(
            f"{base}/SelfAsserted",
            params={"tx": trans_id, "p": policy},
            data=form,
            headers=headers_xhr,
        ) as resp:
            stato_http = resp.status
            tipo = resp.headers.get("Content-Type", "")
            testo = await resp.text()
        try:
            corpo = json.loads(testo)
        except ValueError:
            corpo = None
        if not isinstance(corpo, dict):
            # Non JSON: B2C ha risposto con una pagina (HTML) invece del
            # solito {"status": ...} - tipicamente un errore di
            # transazione/CSRF, non una password sbagliata. Si riporta un
            # estratto (la pagina non contiene credenziali) per capire cosa.
            estratto = " ".join(testo.split())[:300]
            raise SetAuthError(
                f"SelfAsserted ha risposto HTTP {stato_http} ({tipo}) senza JSON: {estratto!r}"
            )
        if str(corpo.get("status")) != "200":
            # B2C risponde HTTP 200 anche in caso di errore, con status "400"
            # e errorCode/message - verificato dal vivo il 30/09/2026:
            # {"status":"400","errorCode":"AADB2C90053","message":"Credenziali non valide."}
            messaggio = corpo.get("message") or corpo.get("errorCode") or corpo
            raise SetInvalidCredentials(f"Login SET rifiutato: {messaggio}")

        # 3. conferma -> 302 con il code nel fragment
        async with self._session.get(
            f"{base}/api/{api}/confirmed",
            params={"rememberMe": "true", "csrf_token": csrf, "tx": trans_id, "p": policy},
            headers=headers_html,
            allow_redirects=False,
        ) as resp:
            if resp.status not in (301, 302, 303, 307, 308):
                # Un 200 qui è quasi certamente un'altra pagina della policy
                # (consenso, MFA, cambio password forzato): non sappiamo
                # gestirla, meglio dirlo chiaramente che fallire più avanti.
                raise SetAuthError(
                    f"Passo 'confirmed' ha risposto HTTP {resp.status} invece di un "
                    "redirect: la policy B2C richiede un passaggio aggiuntivo non gestito?"
                )
            location = resp.headers.get("Location", "")
        code = _code_da_location(location)

        # 4. scambio code -> token
        return await self._token_request(
            {
                "client_id": CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "client_info": "1",
            }
        )

    async def async_refresh(self, refresh_token: str) -> SetTokens:
        """grant_type=refresh_token - verificato da terminale il 30/09/2026
        (scripts/verify_set_login.py --refresh). Solleva SetAuthError se B2C lo rifiuta: il chiamante
        ricade sul login completo."""
        try:
            return await self._token_request(
                {
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": SCOPE,
                    "client_info": "1",
                }
            )
        except aiohttp.ClientError as err:
            raise SetAuthError(f"Errore di trasporto durante il refresh del token: {err}") from err

    async def _token_request(self, dati: dict[str, str]) -> SetTokens:
        headers = {**HEADERS_BROWSER, "Origin": REDIRECT_URI}
        async with self._session.post(TOKEN_URL, data=dati, headers=headers) as resp:
            corpo = await resp.json(content_type=None)
            if resp.status != 200:
                errore = corpo.get("error") if isinstance(corpo, dict) else None
                descrizione = corpo.get("error_description") if isinstance(corpo, dict) else None
                raise SetAuthError(
                    f"Token endpoint B2C HTTP {resp.status}: {errore} - {descrizione}"
                )
        try:
            return SetTokens(
                access_token=corpo["access_token"],
                refresh_token=corpo.get("refresh_token"),
                expires_in=int(corpo.get("expires_in") or 3600),
                refresh_token_expires_in=(
                    int(corpo["refresh_token_expires_in"])
                    if corpo.get("refresh_token_expires_in") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise SetAuthError(
                f"Risposta del token endpoint inattesa (access_token mancante): {corpo!r}"
            ) from err
