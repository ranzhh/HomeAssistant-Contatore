"""Test per distributors/set/auth.py: flusso Azure AD B2C (authorize ->
SelfAsserted -> confirmed -> token) guidato a mano, nessuna chiamata di
rete reale.

Come tests/ireti/test_auth.py, auth.py non dipende da Home Assistant, ma
viene comunque caricato via importlib bypassando i vari __init__.py della
gerarchia (che lo fanno).

Le risposte simulate hanno la forma di quelle reali della cattura del
30/09/2026 (blocco SETTINGS della pagina /authorize, {"status": "200"} di
SelfAsserted, 302 con code nel fragment, JSON del token endpoint), con
tutti i valori sostituiti da valori di fantasia.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import aiohttp
import pytest

SET_DIR = (
    Path(__file__).parent.parent.parent
    / "custom_components"
    / "contatore_letture"
    / "distributors"
    / "set"
)


def _load_auth_module():
    pkg_name = "set_test_auth"
    if f"{pkg_name}.auth" in sys.modules:
        return sys.modules[f"{pkg_name}.auth"]

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(SET_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(modname: str, filename: str):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{modname}", SET_DIR / filename
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{modname}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("const", "const.py")
    return _load("auth", "auth.py")


auth = _load_auth_module()

# --- Risposte simulate -------------------------------------------------------

PAGINA_AUTHORIZE = """<!DOCTYPE html><html><head><script>
var SETTINGS = {"remoteResource":"https://example.invalid/ui.html","retryLimit":3,
"trimSpacesInPassword":true,"api":"CombinedSigninAndSignup","csrf":"csrf-di-fantasia",
"transId":"StateProperties=trans-di-fantasia","pageViewId":"00000000-0000-0000-0000-000000000000",
"suppressElementCss":false,"isPageViewIdSentWithHeader":false,"allowAutoFocusOnPasswordField":true,
"pageMode":1,"config":{"showSignupLink":"false","enableRememberMe":"true","operatingMode":"Email"},
"hosts":{"tenant":"/sportelloclientib2cset.onmicrosoft.com/B2C_1A_signin","policy":"B2C_1A_signin",
"static":"https://sportelloclientib2cset.b2clogin.com/static/"},"locale":{"lang":"it"}};
</script></head><body></body></html>"""

LOCATION_OK = (
    "https://myset.setdistribuzione.it/#state=stato-di-fantasia"
    "&client_info=info-di-fantasia&code=code-di-fantasia"
)

# Forma reale del token endpoint (30/09/2026), valori di fantasia.
TOKEN_REALE = {
    "access_token": "access-di-fantasia",
    "id_token": "id-di-fantasia",
    "token_type": "Bearer",
    "not_before": 1790783740,
    "expires_in": 3600,
    "expires_on": 1790787340,
    "resource": "00000000-0000-0000-0000-000000000000",
    "client_info": "info-di-fantasia",
    "scope": "https://sportelloclientib2cset.onmicrosoft.com/private/api/read",
    "refresh_token": "refresh-di-fantasia",
    "refresh_token_expires_in": 86400,
}


class _Risposta:
    def __init__(self, status: int, corpo=None, testo: str = "", headers=None):
        self.status = status
        self._corpo = corpo
        self._testo = testo
        self.headers = headers or {}

    async def json(self, content_type=None):
        return self._corpo

    async def text(self):
        if self._corpo is not None and not self._testo:
            return json.dumps(self._corpo)
        return self._testo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Sessione:
    """Instrada per pezzo di URL: authorize / SelfAsserted / confirmed /
    token. Ogni risposta e' sovrascrivibile dal singolo test."""

    def __init__(self):
        self.richieste: list[dict] = []
        self.risposte = {
            "authorize": _Risposta(200, testo=PAGINA_AUTHORIZE),
            "SelfAsserted": _Risposta(200, corpo={"status": "200"}),
            "confirmed": _Risposta(302, headers={"Location": LOCATION_OK}),
            "token": _Risposta(200, corpo=TOKEN_REALE),
        }

    def _instrada(self, metodo, url, **kw):
        self.richieste.append({"metodo": metodo, "url": url, **kw})
        for chiave, risposta in self.risposte.items():
            if chiave in url:
                return risposta
        raise AssertionError(f"URL inatteso: {url}")

    def get(self, url, **kw):
        return self._instrada("GET", url, **kw)

    def post(self, url, **kw):
        return self._instrada("POST", url, **kw)


def _richiesta(sessione, pezzo):
    return next(r for r in sessione.richieste if pezzo in r["url"])


class TestAsyncLogin:
    @pytest.mark.asyncio
    async def test_login_riuscito_ritorna_token(self):
        sessione = _Sessione()
        tokens = await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")
        assert tokens.access_token == "access-di-fantasia"
        assert tokens.refresh_token == "refresh-di-fantasia"
        assert tokens.expires_in == 3600
        assert tokens.refresh_token_expires_in == 86400

    @pytest.mark.asyncio
    async def test_sequenza_delle_richieste(self):
        sessione = _Sessione()
        await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")
        assert [r["metodo"] for r in sessione.richieste] == ["GET", "POST", "GET", "POST"]
        assert [
            next(k for k in ("authorize", "SelfAsserted", "confirmed", "token") if k in r["url"])
            for r in sessione.richieste
        ] == ["authorize", "SelfAsserted", "confirmed", "token"]

    @pytest.mark.asyncio
    async def test_authorize_manda_pkce_e_parametri_msal(self):
        sessione = _Sessione()
        await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")
        params = _richiesta(sessione, "authorize")["params"]
        assert params["client_id"] == "3cfe517e-7c9e-433e-a9dd-daf4cca499c9"
        assert params["response_type"] == "code"
        assert params["response_mode"] == "fragment"
        assert params["code_challenge_method"] == "S256"
        assert params["redirect_uri"] == "https://myset.setdistribuzione.it"
        assert "offline_access" in params["scope"]
        assert len(params["code_challenge"]) == 43  # base64url di SHA-256, senza padding

    @pytest.mark.asyncio
    async def test_selfasserted_usa_csrf_e_transid_della_pagina(self):
        sessione = _Sessione()
        await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")
        r = _richiesta(sessione, "SelfAsserted")
        # path con la maiuscola di hosts.tenant, non la policy minuscola di /authorize
        assert "/sportelloclientib2cset.onmicrosoft.com/B2C_1A_signin/SelfAsserted" in r["url"]
        assert r["params"] == {"tx": "StateProperties=trans-di-fantasia", "p": "B2C_1A_signin"}
        assert r["headers"]["X-CSRF-TOKEN"] == "csrf-di-fantasia"
        assert r["headers"]["X-Requested-With"] == "XMLHttpRequest"
        assert r["data"] == {
            "request_type": "RESPONSE",
            "signInName": "utente@example.com",
            "password": "segreto",
        }

    @pytest.mark.asyncio
    async def test_confirmed_non_segue_il_redirect(self):
        sessione = _Sessione()
        await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")
        r = _richiesta(sessione, "confirmed")
        assert r["allow_redirects"] is False
        assert r["params"]["csrf_token"] == "csrf-di-fantasia"
        assert r["params"]["tx"] == "StateProperties=trans-di-fantasia"

    @pytest.mark.asyncio
    async def test_token_scambia_code_con_verifier(self):
        sessione = _Sessione()
        await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")
        dati = _richiesta(sessione, "token")["data"]
        assert dati["grant_type"] == "authorization_code"
        assert dati["code"] == "code-di-fantasia"
        assert dati["code_verifier"]
        assert dati["redirect_uri"] == "https://myset.setdistribuzione.it"

    @pytest.mark.asyncio
    async def test_credenziali_invalide_solleva_eccezione_dedicata(self):
        sessione = _Sessione()
        sessione.risposte["SelfAsserted"] = _Risposta(
            200,
            corpo={"status": "400", "errorCode": "UserNotFound",
                   "message": "Nome utente o password non validi."},
        )
        with pytest.raises(auth.SetInvalidCredentials, match="non validi"):
            await auth.SetAuthClient(sessione).async_login("utente@example.com", "sbagliata")
        # non si va avanti dopo il rifiuto
        assert not any("confirmed" in r["url"] for r in sessione.richieste)

    @pytest.mark.asyncio
    async def test_pagina_senza_settings_solleva_autherror(self):
        sessione = _Sessione()
        sessione.risposte["authorize"] = _Risposta(200, testo="<html>pagina cambiata</html>")
        with pytest.raises(auth.SetAuthError, match="SETTINGS"):
            await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")

    @pytest.mark.asyncio
    async def test_confirmed_senza_redirect_solleva_autherror(self):
        """Un 200 al posto del 302 e' una pagina aggiuntiva della policy
        (MFA, consenso...) che non sappiamo gestire: errore esplicito."""
        sessione = _Sessione()
        sessione.risposte["confirmed"] = _Risposta(200, testo="<html>altro step</html>")
        with pytest.raises(auth.SetAuthError, match="redirect"):
            await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")

    @pytest.mark.asyncio
    async def test_redirect_con_errore_invece_del_code(self):
        sessione = _Sessione()
        sessione.risposte["confirmed"] = _Risposta(
            302,
            headers={"Location": "https://myset.setdistribuzione.it/#error=access_denied"
                     "&error_description=AADB2C90118+password+reset"},
        )
        with pytest.raises(auth.SetAuthError, match="AADB2C90118"):
            await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")

    @pytest.mark.asyncio
    async def test_token_endpoint_in_errore_solleva_autherror(self):
        sessione = _Sessione()
        sessione.risposte["token"] = _Risposta(
            400, corpo={"error": "invalid_grant", "error_description": "AADB2C90080 expired"}
        )
        with pytest.raises(auth.SetAuthError, match="invalid_grant"):
            await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")

    @pytest.mark.asyncio
    async def test_risposta_senza_access_token_solleva_autherror(self):
        sessione = _Sessione()
        sessione.risposte["token"] = _Risposta(200, corpo={"token_type": "Bearer"})
        with pytest.raises(auth.SetAuthError, match="access_token"):
            await auth.SetAuthClient(sessione).async_login("utente@example.com", "segreto")

    @pytest.mark.asyncio
    async def test_errore_di_trasporto_solleva_autherror(self):
        class _SessioneRotta:
            def get(self, url, **kw):
                raise aiohttp.ClientConnectionError("connessione rifiutata")

        with pytest.raises(auth.SetAuthError):
            await auth.SetAuthClient(_SessioneRotta()).async_login("utente@example.com", "segreto")


class TestCookieJar:
    def test_jar_che_quota_i_cookie_viene_rifiutato(self):
        """Il jar di default di aiohttp (quote_cookie=True) fa rispondere
        B2C con 400 a ogni richiesta successiva alla prima - verificato dal
        vivo il 30/09/2026: meglio fallire subito con una spiegazione."""
        sessione = _Sessione()
        sessione.cookie_jar = types.SimpleNamespace(_quote_cookie=True)
        with pytest.raises(auth.SetAuthError, match="quote_cookie"):
            auth.SetAuthClient(sessione)

    @pytest.mark.asyncio
    async def test_jar_corretto_viene_accettato(self):
        sessione = _Sessione()
        sessione.cookie_jar = auth.crea_cookie_jar()
        auth.SetAuthClient(sessione)

    @pytest.mark.asyncio
    async def test_crea_cookie_jar_non_quota(self):
        assert auth.crea_cookie_jar()._quote_cookie is False


class TestAsyncRefresh:
    @pytest.mark.asyncio
    async def test_refresh_manda_grant_type_refresh_token(self):
        sessione = _Sessione()
        tokens = await auth.SetAuthClient(sessione).async_refresh("refresh-di-fantasia")
        dati = _richiesta(sessione, "token")["data"]
        assert dati["grant_type"] == "refresh_token"
        assert dati["refresh_token"] == "refresh-di-fantasia"
        assert dati["client_id"] == "3cfe517e-7c9e-433e-a9dd-daf4cca499c9"
        assert tokens.access_token == "access-di-fantasia"

    @pytest.mark.asyncio
    async def test_refresh_rifiutato_solleva_autherror(self):
        sessione = _Sessione()
        sessione.risposte["token"] = _Risposta(400, corpo={"error": "invalid_grant"})
        with pytest.raises(auth.SetAuthError):
            await auth.SetAuthClient(sessione).async_refresh("scaduto")


class TestHelper:
    def test_code_da_location_nel_fragment(self):
        assert auth._code_da_location(LOCATION_OK) == "code-di-fantasia"

    def test_code_da_location_nella_query(self):
        assert auth._code_da_location("https://x/?code=abc&state=s") == "abc"

    def test_pkce_challenge_e_sha256_del_verifier(self):
        import base64
        import hashlib

        verifier, challenge = auth._pkce()
        atteso = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
        assert challenge == atteso.decode()
