"""Test per distributors/set/api.py: costruzione delle chiamate REST e
parsing delle risposte, nessuna chiamata di rete reale.

Come tests/set/test_auth.py, api.py non dipende da Home Assistant, ma
viene comunque caricato via importlib bypassando i vari __init__.py della
gerarchia (che lo fanno).

I payload hanno la FORMA delle risposte reali della cattura del
30/09/2026 (chiavi, tipi, messaggi del gateway), con i valori
identificativi (POD, contractId, codice fiscale, matricola) sostituiti da
valori di fantasia e i consumi ridotti a pochi elementi.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date
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


def _load_api_module():
    pkg_name = "set_test_api"
    if f"{pkg_name}.api" in sys.modules:
        return sys.modules[f"{pkg_name}.api"]

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
    return _load("api", "api.py")


api = _load_api_module()


class _Risposta:
    def __init__(self, status: int, corpo):
        self.status = status
        self._corpo = corpo

    async def json(self, content_type=None):
        return self._corpo

    async def text(self):
        return str(self._corpo)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Sessione:
    """Ritorna in sequenza le risposte configurate, una per chiamata."""

    def __init__(self, risposte: list[tuple[int, object]]):
        self._risposte = list(risposte)
        self.richieste: list[dict] = []

    def get(self, url, params=None, headers=None):
        self.richieste.append({"metodo": "GET", "url": url, "params": params, "headers": headers})
        status, corpo = self._risposte.pop(0)
        return _Risposta(status, corpo)


def _client(risposte) -> tuple[api.SetApiClient, _Sessione]:
    sessione = _Sessione(risposte)
    return api.SetApiClient(sessione, "token-di-fantasia"), sessione


POD = "IT221E00000000001"
CONTRACT = "0010000001"
CF = "AAABBB00A00A000A"

PAYLOAD_REGISTRATION = {
    "message": "Profile retrieved",
    "data": {
        "email": "utente@example.com",
        "cellphone": "3000000000",
        "givenName": "Nome",
        "surname": "Cognome",
        "fiscalCode": CF,
        "customerType": "RETAIL",
        "profiles": ["Retail_SET"],
        "assistant": False,
        "superAdmin": False,
        "isImpersonating": False,
        "isReadOnly": False,
        "shortDisplayName": "N. Cognome",
        "displayName": "Nome Cognome",
    },
}

PAYLOAD_ACTIVE = {
    "message": "Supplies retrieved",
    "data": {
        "count": 1,
        "profile": "Retail_SET",
        "utilities": [
            {
                "businessPartner": "0000000001",
                "name": "Casa",
                "utilityStatus": "ACTIVE",
                "podPdr": POD,
                "utilityType": "ENERGY",
                "utilityAddress": "VIA DI FANTASIA 1, PAESE",
                "contractId": CONTRACT,
                "pesseGroup": "7",
            }
        ],
        "utilityTypes": ["ENERGY"],
    },
}


def _ora(hour: int, quarters: list[float]) -> dict:
    return {
        "drillDownAvailable": True,
        "year": 2026,
        "month": 9,
        "day": 20,
        "hour": hour,
        "quarters": quarters,
        "total": round(sum(quarters), 3),
        "average": round(sum(quarters) / 4, 5),
        "estimated": False,
        "meterSerialNumber": "matricola-di-fantasia",
        "kConstant": "1",
    }


PAYLOAD_GIORNO = {
    "message": "Utility's consumptions retrieved",
    "data": {
        "consumptions": [_ora(0, [0.117, 0.124, 0.289, 0.046]), _ora(1, [0.036, 0.034, 0.035, 0.035])],
        "total": 0.716,
        "average": 0.358,
        "powerPeak": "domenica 20 ore 13: 3,620 kW",
    },
}

PAYLOAD_MESE = {
    "message": "Utility's consumptions retrieved",
    "data": {
        "consumptions": [
            {"drillDownAvailable": True, "year": 2026, "month": 9, "day": 1, "hour": 0,
             "total": 3.453999, "average": 0.14391662, "estimated": False,
             "meterSerialNumber": "matricola-di-fantasia", "kConstant": "1"},
        ],
        "total": 370.757,
        "average": 12.784723,
        "powerPeak": "domenica 20 ore 13: 3,620 kW",
    },
}


class TestScegliProfilo:
    def test_unico_profilo(self):
        assert api.scegli_profilo(["Retail_SET"]) == "Retail_SET"

    def test_preferisce_il_profilo_set(self):
        assert api.scegli_profilo(["Retail_GAS", "Business_SET"]) == "Business_SET"

    def test_senza_set_prende_il_primo(self):
        assert api.scegli_profilo(["Prospect"]) == "Prospect"

    def test_vuoto_solleva_errore(self):
        with pytest.raises(api.SetApiError):
            api.scegli_profilo([])


class TestAnagrafica:
    @pytest.mark.asyncio
    async def test_registration_ritorna_data(self):
        client, sessione = _client([(200, PAYLOAD_REGISTRATION)])
        dati = await client.async_get_registration()
        assert dati["fiscalCode"] == CF
        assert dati["profiles"] == ["Retail_SET"]
        assert sessione.richieste[0]["url"].endswith("/secure/profile/registration")
        assert sessione.richieste[0]["headers"]["Authorization"] == "Bearer token-di-fantasia"

    @pytest.mark.asyncio
    async def test_registration_senza_fiscalcode_solleva_errore(self):
        client, _ = _client([(200, {"message": "x", "data": {}})])
        with pytest.raises(api.SetApiError, match="fiscalCode"):
            await client.async_get_registration()

    @pytest.mark.asyncio
    async def test_active_ritorna_forniture_elettriche_attive(self):
        client, sessione = _client([(200, PAYLOAD_ACTIVE)])
        utilities = await client.async_get_active_utilities(CF, "Retail_SET")
        assert [u["podPdr"] for u in utilities] == [POD]
        assert utilities[0]["contractId"] == CONTRACT
        assert sessione.richieste[0]["url"].endswith(f"/secure/utility/{CF}/active")
        assert sessione.richieste[0]["params"] == {"profile": "Retail_SET"}

    @pytest.mark.asyncio
    async def test_active_scarta_gas_e_cessate(self):
        payload = {
            "message": "Supplies retrieved",
            "data": {
                "utilities": [
                    {**PAYLOAD_ACTIVE["data"]["utilities"][0], "utilityType": "GAS",
                     "podPdr": "00000000000001"},
                    {**PAYLOAD_ACTIVE["data"]["utilities"][0], "utilityStatus": "CEASED",
                     "podPdr": "IT221E00000000009"},
                    PAYLOAD_ACTIVE["data"]["utilities"][0],
                ]
            },
        }
        client, _ = _client([(200, payload)])
        utilities = await client.async_get_active_utilities(CF, "Retail_SET")
        assert [u["podPdr"] for u in utilities] == [POD]

    @pytest.mark.asyncio
    async def test_active_vuoto_ritorna_lista_vuota_non_errore(self):
        """Account "Prospect": lo stato di partenza di questa ricerca - non
        e' un errore dell'integrazione."""
        client, _ = _client([(200, {"message": "Supplies retrieved", "data": {"count": 0, "utilities": []}})])
        assert await client.async_get_active_utilities(CF, "Prospect") == []


# Forma reale di un giorno NON ancora pubblicato (verificato il 30/09/2026
# chiedendo il giorno stesso): 24 segnaposto senza quarters.
SEGNAPOSTO_GIORNO_NON_PUBBLICATO = [
    {"year": 2026, "month": 9, "day": 30, "hour": h, "total": 0.0, "estimated": True}
    for h in range(24)
]


class TestGiornoPubblicato:
    def test_giorno_reale(self):
        assert api.giorno_pubblicato(PAYLOAD_GIORNO["data"]["consumptions"]) is True

    def test_segnaposto_non_pubblicato(self):
        assert api.giorno_pubblicato(SEGNAPOSTO_GIORNO_NON_PUBBLICATO) is False

    def test_lista_vuota(self):
        assert api.giorno_pubblicato([]) is False

    def test_quarters_vuoti(self):
        assert api.giorno_pubblicato([{"hour": 0, "quarters": [], "total": 0.0}]) is False


class TestConsumption:
    @pytest.mark.asyncio
    async def test_vista_giorno_manda_mese_zero_based(self):
        """Il parametro month e' 0-based (il frontend fa value-1 e la
        cattura lo conferma: month=8 -> settembre). Il campo month nella
        risposta e' invece 1-based."""
        client, sessione = _client([(200, PAYLOAD_GIORNO)])
        ore = await client.async_get_consumption_day(CF, CONTRACT, POD, "Retail_SET", date(2026, 9, 20))
        assert len(ore) == 2
        assert ore[0]["quarters"] == [0.117, 0.124, 0.289, 0.046]
        r = sessione.richieste[0]
        assert r["url"].endswith(f"/secure/utility/{CF}/{CONTRACT}/consumption")
        assert r["params"] == {
            "consumptionRange": "F1_F2_F3",
            "consumptionType": "A1",
            "profile": "Retail_SET",
            "supplyPoint": POD,
            "year": 2026,
            "month": 8,
            "day": 20,
        }

    @pytest.mark.asyncio
    async def test_vista_mese(self):
        client, sessione = _client([(200, PAYLOAD_MESE)])
        giorni = await client.async_get_consumption_month(CF, CONTRACT, POD, "Retail_SET", 2026, 9)
        assert giorni[0]["day"] == 1
        assert sessione.richieste[0]["params"]["month"] == 8
        assert "day" not in sessione.richieste[0]["params"]

    @pytest.mark.asyncio
    async def test_consumptions_assente_ritorna_lista_vuota(self):
        """Giorno non ancora pubblicato: stato normale, non un errore."""
        client, _ = _client([(200, {"message": "Utility's consumptions retrieved", "data": {"consumptions": None}})])
        assert await client.async_get_consumption_day(CF, CONTRACT, POD, "Retail_SET", date(2026, 9, 30)) == []

    @pytest.mark.asyncio
    async def test_404_sulle_viste_ritorna_lista_vuota(self):
        """Anno/mese senza nessun dato: il gateway risponde 404
        {"statusCode": 404, "message": "Resource not found"} (verificato
        sulla vista anno 2023) - non e' un errore."""
        corpo_404 = {"statusCode": 404, "message": "Resource not found"}
        client, _ = _client([(404, corpo_404), (404, corpo_404), (404, corpo_404)])
        assert await client.async_get_consumption_year(CF, CONTRACT, POD, "Retail_SET", 2023) == []
        assert await client.async_get_consumption_month(CF, CONTRACT, POD, "Retail_SET", 2023, 5) == []
        assert await client.async_get_consumption_day(CF, CONTRACT, POD, "Retail_SET", date(2023, 5, 1)) == []

    @pytest.mark.asyncio
    async def test_404_su_registration_resta_un_errore(self):
        client, _ = _client([(404, {"statusCode": 404})])
        with pytest.raises(api.SetApiError):
            await client.async_get_registration()

    @pytest.mark.asyncio
    async def test_vista_anno(self):
        payload = {"message": "Utility's consumptions retrieved", "data": {"consumptions": [
            {"drillDownAvailable": True, "year": 2026, "month": 1, "total": 399.59, "average": 0.0,
             "descMonth": "January", "estimated": False, "meterSerialNumber": "matricola-di-fantasia"}],
            "total": 2741.6472, "average": 304.62747}}
        client, sessione = _client([(200, payload)])
        mesi = await client.async_get_consumption_year(CF, CONTRACT, POD, "Retail_SET", 2026)
        assert mesi[0]["month"] == 1 and mesi[0]["drillDownAvailable"] is True
        assert sessione.richieste[0]["params"] == {
            "consumptionRange": "F1_F2_F3", "consumptionType": "A1", "profile": "Retail_SET",
            "supplyPoint": POD, "year": 2026,
        }

    @pytest.mark.asyncio
    async def test_401_solleva_unauthorized(self):
        client, _ = _client([(401, {"message": "Unauthorized"})])
        with pytest.raises(api.SetApiUnauthorized):
            await client.async_get_consumption_day(CF, CONTRACT, POD, "Retail_SET", date(2026, 9, 20))

    @pytest.mark.asyncio
    async def test_http_error_solleva_apierror(self):
        client, _ = _client([(500, {"message": "boom"})])
        with pytest.raises(api.SetApiError, match="500"):
            await client.async_get_consumption_day(CF, CONTRACT, POD, "Retail_SET", date(2026, 9, 20))

    @pytest.mark.asyncio
    async def test_risposta_senza_data_solleva_apierror(self):
        client, _ = _client([(200, {"message": "strano"})])
        with pytest.raises(api.SetApiError, match="data"):
            await client.async_get_registration()

    @pytest.mark.asyncio
    async def test_errore_di_trasporto_solleva_apierror(self):
        class _SessioneRotta:
            def get(self, url, params=None, headers=None):
                raise aiohttp.ClientConnectionError("connessione rifiutata")

        client = api.SetApiClient(_SessioneRotta(), "token")
        with pytest.raises(api.SetApiError):
            await client.async_get_registration()
