"""Client per le API del gateway SET (agw.setdistribuzione.it/api).

Tutte via `Authorization: Bearer <access_token>` (auth.py) - verificato
sulla cattura Chrome del 30/09/2026 (nessun header custom, nessuna
subscription key, nessun cookie). Endpoint e forma delle risposte
documentati per esteso in documentation/protocols/set-distribuzione-protocol.md.

Stato di conferma per endpoint:
  - /secure/profile/registration: confermato (dà fiscalCode e profiles).
  - /secure/utility/{fiscalCode}/active: confermato - elenco delle
    forniture con podPdr + contractId, la fonte per la scoperta dei POD.
  - /secure/utility/{fiscalCode}/{contractId}/consumption: confermato
    nelle tre viste (anno -> mesi, mese -> giorni, giorno -> ore con 4
    quarti d'ora ciascuna). La vista giorno è la fonte primaria.

Ogni risposta ha la forma {"message": "...", "data": ...}: qui si torna
sempre e solo `data`.

ATTENZIONE al parametro `month`: è 0-based (gennaio = 0). Lo fa il
frontend (`value -= 1` prima della chiamata, visibile nel bundle) e lo
conferma la cattura: month=8 ha restituito i giorni di settembre. Il
campo `month` DENTRO la risposta è invece 1-based. I metodi di questo
client accettano sempre mesi 1-based e fanno la conversione da soli.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import aiohttp

from .const import (
    BASE_API,
    CONSUMPTION_RANGE,
    CONSUMPTION_TYPE_ATTIVA,
    HEADERS_BROWSER,
    UTILITY_STATUS_ATTIVA,
    UTILITY_TYPE_ENERGIA,
)

_LOGGER = logging.getLogger(__name__)


class SetApiError(Exception):
    """Chiamata fallita (trasporto, HTTP non-200, o risposta inattesa)."""


class SetApiUnauthorized(SetApiError):
    """HTTP 401: access_token scaduto o revocato - il chiamante deve
    rifare login/refresh, non riprovare la stessa chiamata."""


def scegli_profilo(profiles: list[str]) -> str:
    """Il profilo mySET da usare nel parametro `profile` di ogni chiamata.

    Su un account SET puro c'è un solo profilo ("Retail_SET" osservato;
    "Business_SET" esiste nell'enum del bundle). Se ce n'è più di uno
    (account del gruppo con anche myDOLOMITI/gas), si preferisce quello
    con suffisso _SET; altrimenti il primo. Un elenco vuoto è il caso
    "Prospect senza fornitura" ed è un errore per noi."""
    if not profiles:
        raise SetApiError("Nessun profilo mySET sull'account (profiles vuoto)")
    for p in profiles:
        if isinstance(p, str) and p.endswith("_SET"):
            return p
    return profiles[0]


class SetApiClient:
    def __init__(self, session: aiohttp.ClientSession, access_token: str) -> None:
        self._session = session
        self._headers = {**HEADERS_BROWSER, "Authorization": f"Bearer {access_token}"}

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, none_su_404: bool = False
    ) -> Any:
        """GET su {BASE_API}{path}: ritorna `data`. Con none_su_404=True un
        HTTP 404 ritorna None invece di sollevare: il gateway risponde
        404 {"statusCode": 404, "message": "Resource not found"} per un
        anno senza nessun dato (verificato: vista anno 2023 su un contatore
        con storico dal 2024), non e' un errore."""
        try:
            async with self._session.get(
                f"{BASE_API}{path}", params=params, headers=self._headers
            ) as resp:
                if resp.status == 401:
                    raise SetApiUnauthorized(f"GET {path}: HTTP 401")
                if resp.status == 404 and none_su_404:
                    return None
                if resp.status != 200:
                    testo = (await resp.text())[:300]
                    raise SetApiError(f"GET {path}: HTTP {resp.status} {testo!r}")
                corpo = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise SetApiError(f"Errore di trasporto chiamando GET {path}: {err}") from err
        if not isinstance(corpo, dict) or "data" not in corpo:
            raise SetApiError(f"Risposta inattesa da GET {path} (manca 'data'): {corpo!r}")
        return corpo["data"]

    async def async_get_registration(self) -> dict[str, Any]:
        """GET /secure/profile/registration - anagrafica dell'utente
        loggato: `fiscalCode`, `profiles` (lista), `customerType`."""
        dati = await self._get("/secure/profile/registration")
        if not isinstance(dati, dict) or not dati.get("fiscalCode"):
            raise SetApiError(f"'fiscalCode' mancante nella risposta registration: {dati!r}")
        return dati

    async def async_get_active_utilities(
        self, fiscal_code: str, profile: str
    ) -> list[dict[str, Any]]:
        """GET /secure/utility/{fiscalCode}/active?profile= - forniture
        attive dell'account. Ritorna SOLO quelle elettriche e attive
        (utilityType ENERGY, utilityStatus ACTIVE), ciascuna con almeno
        `podPdr` e `contractId`. Lista vuota se non ce ne sono (account
        "Prospect": stato normale, non un errore)."""
        dati = await self._get(f"/secure/utility/{fiscal_code}/active", {"profile": profile})
        utilities = dati.get("utilities") if isinstance(dati, dict) else None
        risultato = []
        for u in utilities or []:
            if not isinstance(u, dict) or not u.get("podPdr") or not u.get("contractId"):
                continue
            if u.get("utilityType") != UTILITY_TYPE_ENERGIA:
                continue
            if u.get("utilityStatus") not in (None, UTILITY_STATUS_ATTIVA):
                continue
            risultato.append(u)
        return risultato

    def _params_consumo(self, pod: str, profile: str, **extra: Any) -> dict[str, Any]:
        return {
            "consumptionRange": CONSUMPTION_RANGE,
            "consumptionType": CONSUMPTION_TYPE_ATTIVA,
            "profile": profile,
            "supplyPoint": pod,
            **extra,
        }

    async def async_get_consumption_day(
        self, fiscal_code: str, contract_id: str, pod: str, profile: str, giorno: date
    ) -> list[dict[str, Any]]:
        """Vista "giorno": 24 elementi (uno per ora, `hour` 0-23), ciascuno
        con `quarters` (4 valori kWh a 15 minuti), `total`, `average`,
        `estimated`, `kConstant`, `meterSerialNumber`.

        ATTENZIONE: un giorno NON ancora pubblicato NON torna vuoto -
        torna comunque 24 elementi segnaposto `{"year","month","day",
        "hour","total": 0.0,"estimated": true}` SENZA `quarters` (verificato
        il 30/09/2026 chiedendo il giorno stesso). Il chiamante deve usare
        `giorno_pubblicato()` prima di importare, altrimenti scriverebbe
        24 ore a zero e considererebbe il giorno fatto. Un anno/mese senza
        nessun dato risponde invece 404 -> lista vuota."""
        dati = await self._get(
            f"/secure/utility/{fiscal_code}/{contract_id}/consumption",
            self._params_consumo(
                pod, profile, year=giorno.year, month=giorno.month - 1, day=giorno.day
            ),
            none_su_404=True,
        )
        return list((dati or {}).get("consumptions") or []) if isinstance(dati, dict) else []

    async def async_get_consumption_year(
        self, fiscal_code: str, contract_id: str, pod: str, profile: str, anno: int
    ) -> list[dict[str, Any]]:
        """Vista "anno": un elemento per mese (`month` 1-based, `total`,
        `estimated`, `drillDownAvailable`). `drillDownAvailable` dice se per
        quel mese esiste il dettaglio giorni/quarti d'ora: sul contatore
        della cattura il 2025 ha 12 mesi tutti drillabili, il 2024 11 mesi
        di cui solo 3 drillabili (contatore 2G da ottobre 2024), il 2023
        risponde 404 -> lista vuota. Usata da recupera_storico per non
        chiedere 30 giorni a vuoto per ogni mese senza dettaglio."""
        dati = await self._get(
            f"/secure/utility/{fiscal_code}/{contract_id}/consumption",
            self._params_consumo(pod, profile, year=anno),
            none_su_404=True,
        )
        return list((dati or {}).get("consumptions") or []) if isinstance(dati, dict) else []

    async def async_get_consumption_month(
        self, fiscal_code: str, contract_id: str, pod: str, profile: str, anno: int, mese: int
    ) -> list[dict[str, Any]]:
        """Vista "mese" (mese 1-based): un elemento per giorno pubblicato
        (`day`, `total` kWh del giorno, `estimated`). Non è usata dal
        ciclo automatico (non ha i quarti d'ora) ma è comoda per capire
        quali giorni esistono senza 30 chiamate."""
        dati = await self._get(
            f"/secure/utility/{fiscal_code}/{contract_id}/consumption",
            self._params_consumo(pod, profile, year=anno, month=mese - 1),
            none_su_404=True,
        )
        return list((dati or {}).get("consumptions") or []) if isinstance(dati, dict) else []


def giorno_pubblicato(consumptions: list[dict[str, Any]]) -> bool:
    """True se la vista giorno contiene dati reali: almeno un elemento con
    `quarters` non vuoto. I 24 segnaposto di un giorno non ancora
    pubblicato (total 0.0, estimated true, niente quarters) danno False,
    come una lista vuota."""
    return any(
        isinstance(e, dict) and isinstance(e.get("quarters"), list) and e["quarters"]
        for e in consumptions
    )
