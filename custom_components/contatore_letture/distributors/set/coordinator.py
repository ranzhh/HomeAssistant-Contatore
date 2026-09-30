"""DataUpdateCoordinator per SET Distribuzione.

Design completo (e il "perché") in
documentation/protocols/set-distribuzione-protocol.md, sezione "Design del
coordinator". In sintesi:

  - La curva a 15 minuti si ottiene SOLO dalla vista "giorno" dell'endpoint
    di consumo (year+month+day): UNA chiamata per giorno, nessun intervallo
    arbitrario come Ireti. La coda dei giorni da riprovare (per POD, stesso
    meccanismo di edistribuzione/ireti: abbandono a tempo, non a conteggio
    tentativi) è quindi anche il tetto alle chiamate di un ciclo.

  - Ciclo ORARIO con orario di cortesia (CONF_ORA_RICHIESTA, default
    ORA_MINIMA_RICHIESTA): prima di quell'ora si chiedono solo gli
    eventuali arretrati, non il giorno precedente. Un ciclo senza nulla da
    chiedere non fa nemmeno il login: costo zero lato portale.

  - Token tenuti in memoria (persi a un riavvio, ririsolti al primo
    ciclo utile): access_token 1h, refresh_token 24h. Si usa il refresh
    finché vale, e si rifà il login B2C completo (5 richieste) solo quando
    serve o se il refresh fallisce. Credenziali non valide sollevano
    ConfigEntryAuthFailed (HA gestisce da solo il reauth), non un
    semplice UpdateFailed.

  - fiscalCode/profile vengono dalla config entry (risolti nel config
    flow); i contractId per POD anche, ma se mancano (entry di una
    versione precedente, POD aggiunto dalle opzioni) si ririsolvono da
    /active e si persistono.
"""
from __future__ import annotations

import logging
import time as time_module
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ...const import DOMAIN
from .api import (
    SetApiClient,
    SetApiError,
    SetApiUnauthorized,
    giorno_pubblicato,
    scegli_profilo,
)
from .auth import (
    SetAuthClient,
    SetAuthError,
    SetInvalidCredentials,
    SetTokens,
    crea_cookie_jar,
)
from .const import (
    ABBANDONO_CODA_DOPO_GIORNI,
    CONF_CONTRACT_IDS,
    CONF_EMAIL,
    CONF_FISCAL_CODE,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_ORA_RICHIESTA,
    CONF_PASSWORD,
    CONF_PODS,
    CONF_PROFILE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    MARGINE_SCADENZA_TOKEN_SECONDI,
    MAX_GIORNI_IN_CODA,
    MAX_GIORNI_RECUPERO_STORICO,
    ORA_MINIMA_RICHIESTA,
    RITARDO_DATI_GIORNI,
)
from .excel import SetExcelError, parse_excel_mensile
from .statistics import (
    async_get_ultima_data_disponibile,
    async_import_curva_giorno,
    kwh_del_giorno,
)

_LOGGER = logging.getLogger(__name__)

# Margine (in giorni) entro cui si risale dagli arretrati in coda: come
# edistribuzione/ireti (150), in pratica mai raggiunto perché la coda tiene
# al massimo MAX_GIORNI_IN_CODA giorni.
_MARGINE_ARRETRATI_GIORNI = 150


def _giorni_nel_periodo(data_da: date, data_a: date) -> list[date]:
    """Elenco dei giorni compresi nell'intervallo, estremi inclusi."""
    giorni, cursore = [], data_da
    while cursore <= data_a:
        giorni.append(cursore)
        cursore += timedelta(days=1)
    return giorni


class SetCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} (SET Distribuzione)",
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES),
            config_entry=entry,
        )
        self.entry = entry
        self.pods: list[str] = list(entry.data[CONF_PODS])
        # cookie_jar: vedi auth.crea_cookie_jar - senza, B2C risponde 400.
        self._session = async_create_clientsession(hass, cookie_jar=crea_cookie_jar())
        self._auth = SetAuthClient(self._session)
        # Cache dei token in memoria (vedi docstring del modulo).
        self._tokens: SetTokens | None = None
        self._access_scade_a: float = 0.0
        self._refresh_scade_a: float = 0.0
        # Identificativi risolti dalla entry o, se mancano, dalle API.
        self._fiscal_code: str | None = entry.data.get(CONF_FISCAL_CODE)
        self._profile: str | None = entry.data.get(CONF_PROFILE)
        self._contract_ids: dict[str, str] = dict(entry.data.get(CONF_CONTRACT_IDS) or {})

    # ------------------------------------------------------------------
    # Login / token
    # ------------------------------------------------------------------

    def _memorizza_token(self, tokens: SetTokens) -> None:
        adesso = time_module.monotonic()
        self._tokens = tokens
        self._access_scade_a = adesso + tokens.expires_in - MARGINE_SCADENZA_TOKEN_SECONDI
        if tokens.refresh_token and tokens.refresh_token_expires_in:
            self._refresh_scade_a = (
                adesso + tokens.refresh_token_expires_in - MARGINE_SCADENZA_TOKEN_SECONDI
            )
        else:
            self._refresh_scade_a = 0.0

    async def _async_login(self, forza: bool = False) -> SetApiClient:
        """Client API con un access_token valido: dalla cache se ancora
        buono, altrimenti via refresh, altrimenti login completo."""
        adesso = time_module.monotonic()
        if not forza and self._tokens and adesso < self._access_scade_a:
            return SetApiClient(self._session, self._tokens.access_token)

        if not forza and self._tokens and self._tokens.refresh_token and adesso < self._refresh_scade_a:
            try:
                self._memorizza_token(await self._auth.async_refresh(self._tokens.refresh_token))
                return SetApiClient(self._session, self._tokens.access_token)
            except SetAuthError as err:
                _LOGGER.info("Refresh del token SET fallito (%s): rifaccio il login completo", err)

        try:
            tokens = await self._auth.async_login(
                self.entry.data[CONF_EMAIL], self.entry.data[CONF_PASSWORD]
            )
        except SetInvalidCredentials as err:
            self._tokens = None
            raise ConfigEntryAuthFailed(f"Credenziali SET non valide: {err}") from err
        except SetAuthError as err:
            raise UpdateFailed(f"Login SET fallito: {err}") from err
        self._memorizza_token(tokens)
        return SetApiClient(self._session, tokens.access_token)

    async def _async_identita(self, api: SetApiClient) -> tuple[str, str]:
        """(fiscal_code, profile) - dalla entry, o risolti da
        /profile/registration la prima volta che mancano."""
        if self._fiscal_code is None or self._profile is None:
            registrazione = await api.async_get_registration()
            self._fiscal_code = registrazione["fiscalCode"]
            self._profile = scegli_profilo(list(registrazione.get("profiles") or []))
        return self._fiscal_code, self._profile

    async def _async_contract_id(self, api: SetApiClient, pod: str) -> str:
        """contractId del POD: dalla entry, o ririsolto da /active (e
        persistito) se manca."""
        if pod in self._contract_ids:
            return self._contract_ids[pod]

        fiscal_code, profile = await self._async_identita(api)
        utilities = await api.async_get_active_utilities(fiscal_code, profile)
        trovati = {u["podPdr"]: str(u["contractId"]) for u in utilities}
        if pod not in trovati:
            raise SetApiError(
                f"Il POD {pod} non risulta tra le forniture attive dell'account "
                f"(trovate: {', '.join(trovati) or 'nessuna'})"
            )
        self._contract_ids.update(trovati)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_CONTRACT_IDS: dict(self._contract_ids)}
        )
        return trovati[pod]

    async def _async_vista_giorno(
        self, api: SetApiClient, pod: str, giorno: date
    ) -> tuple[SetApiClient, list[dict[str, Any]]]:
        """Vista giorno con UN tentativo di re-login su 401 (token scaduto
        o revocato lato B2C prima della scadenza nominale). Ritorna anche
        il client, eventualmente rinnovato, da riusare per le chiamate
        successive dello stesso ciclo."""
        fiscal_code, profile = await self._async_identita(api)
        contract_id = await self._async_contract_id(api, pod)
        try:
            return api, await api.async_get_consumption_day(
                fiscal_code, contract_id, pod, profile, giorno
            )
        except SetApiUnauthorized:
            _LOGGER.info("Token SET rifiutato (401): rifaccio il login e riprovo una volta")
            api = await self._async_login(forza=True)
            return api, await api.async_get_consumption_day(
                fiscal_code, contract_id, pod, profile, giorno
            )

    # ------------------------------------------------------------------
    # Coda dei giorni da riprovare, PER POD (stesso meccanismo di
    # edistribuzione/ireti - vedi quei file per i commenti estesi).
    # ------------------------------------------------------------------

    def _leggi_code(self) -> dict[str, dict[str, date]]:
        grezzo = self.entry.data.get(CONF_GIORNI_DA_RIPROVARE) or {}
        oggi = dt_util.now().date()

        def _con_date(coda: dict) -> dict[str, date]:
            risultato: dict[str, date] = {}
            for giorno, valore in coda.items():
                try:
                    risultato[giorno] = date.fromisoformat(valore)
                except (TypeError, ValueError):
                    risultato[giorno] = oggi
            return risultato

        return {pod: _con_date(coda) for pod, coda in grezzo.items()}

    def _scrivi_code(self, code: dict[str, dict[str, date]]) -> None:
        oggi = dt_util.now().date()
        pulite: dict[str, dict[str, str]] = {}
        for pod, coda in code.items():
            pulita = {
                giorno: da
                for giorno, da in coda.items()
                if (oggi - da).days < ABBANDONO_CODA_DOPO_GIORNI
            }
            abbandonati = set(coda) - set(pulita)
            if abbandonati:
                _LOGGER.warning(
                    "POD %s: giorni abbandonati dopo %d giorni in coda senza dati da "
                    "SET: %s. Se servono, richiedili con l'azione "
                    "contatore_letture.recupera_storico.",
                    pod,
                    ABBANDONO_CODA_DOPO_GIORNI,
                    ", ".join(sorted(abbandonati)),
                )

            if len(pulita) > MAX_GIORNI_IN_CODA:
                tenuti = sorted(pulita, reverse=True)[:MAX_GIORNI_IN_CODA]
                scartati = set(pulita) - set(tenuti)
                _LOGGER.warning(
                    "POD %s: coda dei giorni da riprovare oltre %d elementi: scarto i "
                    "più vecchi (%s)",
                    pod,
                    MAX_GIORNI_IN_CODA,
                    ", ".join(sorted(scartati)),
                )
                pulita = {g: pulita[g] for g in tenuti}

            if pulita:
                pulite[pod] = {g: da.isoformat() for g, da in pulita.items()}

        if pulite != self.entry.data.get(CONF_GIORNI_DA_RIPROVARE):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_GIORNI_DA_RIPROVARE: pulite},
            )

    def _accoda_giorno(self, pod: str, giorno: date) -> None:
        code = self._leggi_code()
        coda = code.setdefault(pod, {})
        chiave = giorno.isoformat()
        if chiave in coda:
            _LOGGER.info(
                "POD %s: giorno %s ancora senza dati da SET, in coda da %d giorni (max %d)",
                pod,
                chiave,
                (dt_util.now().date() - coda[chiave]).days,
                ABBANDONO_CODA_DOPO_GIORNI,
            )
        else:
            coda[chiave] = dt_util.now().date()
            _LOGGER.info(
                "POD %s: giorno %s senza dati da SET, messo in coda per riprovare "
                "(max %d giorni)",
                pod,
                chiave,
                ABBANDONO_CODA_DOPO_GIORNI,
            )
        self._scrivi_code(code)

    def _rimuovi_dalla_coda(self, pod: str, giorni: list[date]) -> None:
        code = self._leggi_code()
        coda = code.get(pod, {})
        rimossi = [g.isoformat() for g in giorni if g.isoformat() in coda]
        if not rimossi:
            return
        for chiave in rimossi:
            del coda[chiave]
        _LOGGER.info("POD %s: dati ricevuti per %s, rimossi dalla coda", pod, ", ".join(rimossi))
        self._scrivi_code(code)

    def _ora_richiesta(self) -> int:
        return int(self.entry.options.get(CONF_ORA_RICHIESTA, ORA_MINIMA_RICHIESTA))

    async def _prossima_richiesta(self, pod: str) -> list[date]:
        """Giorni da chiedere per questo POD in questo ciclo (lista vuota
        se non c'è nulla da fare).

        Stessa logica di edistribuzione/ireti, ma il risultato è una lista
        di giorni (una chiamata ciascuno) invece di un intervallo: dagli
        arretrati in coda fino al giorno atteso (oggi - RITARDO_DATI_GIORNI),
        che però si chiede solo dall'orario di cortesia in poi - prima di
        quell'ora si processano solo gli arretrati."""
        adesso = dt_util.now()
        oggi = adesso.date()
        atteso = oggi - timedelta(days=RITARDO_DATI_GIORNI)
        if adesso.hour < self._ora_richiesta():
            atteso -= timedelta(days=1)

        code = self._leggi_code()
        coda = code.get(pod, {})
        arretrati = sorted(date.fromisoformat(g) for g in coda if date.fromisoformat(g) <= atteso)

        ultima_disponibile = await async_get_ultima_data_disponibile(self.hass, pod)
        if not arretrati and ultima_disponibile and ultima_disponibile >= atteso:
            return []

        if arretrati:
            inizio = max(arretrati[0], atteso - timedelta(days=_MARGINE_ARRETRATI_GIORNI))
            giorni = _giorni_nel_periodo(inizio, atteso)
            # Tra il più vecchio arretrato e il giorno atteso si chiedono
            # solo i giorni in coda o non ancora coperti dalle statistiche:
            # non quelli già importati in mezzo.
            in_coda = {date.fromisoformat(g) for g in coda}
            return [
                g for g in giorni
                if g in in_coda or ultima_disponibile is None or g > ultima_disponibile
            ]

        return [atteso]

    # ------------------------------------------------------------------
    # Ciclo di polling automatico
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        richieste = {pod: await self._prossima_richiesta(pod) for pod in self.pods}

        api: SetApiClient | None = None
        if any(richieste.values()):
            api = await self._async_login()

        by_pod: dict[str, dict] = {}
        for pod in self.pods:
            giorni = richieste[pod]
            kwh_ultimo_giorno_importato = None
            ultimo_giorno_importato = None

            for indice, giorno in enumerate(giorni):
                try:
                    api, vista = await self._async_vista_giorno(api, pod, giorno)
                except (SetApiError, SetAuthError) as err:
                    # I giorni non ancora chiesti vanno in coda invece di
                    # andare persi: al ciclo successivo 'atteso' sarebbe
                    # già avanzato.
                    for rimasto in giorni[indice:]:
                        self._accoda_giorno(pod, rimasto)
                    raise UpdateFailed(
                        f"Errore recuperando la curva SET per il POD {pod}, giorno {giorno}: {err}"
                    ) from err

                if not giorno_pubblicato(vista):
                    # Vuoto, 404, o i 24 segnaposto "estimated" di un giorno
                    # non ancora pubblicato (vedi api.py): in coda.
                    self._accoda_giorno(pod, giorno)
                    continue

                await async_import_curva_giorno(self.hass, pod, giorno, vista)
                self._rimuovi_dalla_coda(pod, [giorno])
                if ultimo_giorno_importato is None or giorno.isoformat() > ultimo_giorno_importato:
                    ultimo_giorno_importato = giorno.isoformat()
                    kwh_ultimo_giorno_importato = kwh_del_giorno(vista)

            ultima_data_disponibile = await async_get_ultima_data_disponibile(self.hass, pod)
            by_pod[pod] = {
                "ultimo_giorno_importato": ultimo_giorno_importato,
                "kwh_ultimo_giorno_importato": kwh_ultimo_giorno_importato,
                "ultima_data_disponibile": (
                    ultima_data_disponibile.isoformat() if ultima_data_disponibile else None
                ),
            }

        return {"by_pod": by_pod}

    async def _async_mesi_senza_dettaglio(
        self, api: SetApiClient, pod: str, giorni: list[date]
    ) -> set[tuple[int, int]]:
        """(anno, mese) toccati da `giorni` per cui la vista anno dice che
        NON c'e' il dettaglio (drillDownAvailable false) o l'anno non
        esiste (404): una chiamata per anno risparmia fino a 30 chiamate a
        vuoto per ogni mese senza curva (contatore 1G, periodo precedente
        all'installazione del 2G...). Se la vista anno fallisce per altri
        motivi non si salta nulla: meglio qualche chiamata in piu' che
        perdere dati."""
        anni = sorted({g.year for g in giorni})
        mesi_richiesti = {(g.year, g.month) for g in giorni}
        fiscal_code, profile = await self._async_identita(api)
        contract_id = await self._async_contract_id(api, pod)
        da_saltare: set[tuple[int, int]] = set()
        for anno in anni:
            try:
                mesi = await api.async_get_consumption_year(
                    fiscal_code, contract_id, pod, profile, anno
                )
            except SetApiError as err:
                _LOGGER.debug("Vista anno %d non disponibile (%s): non salto nulla", anno, err)
                continue
            drillabili = {
                int(m["month"]) for m in mesi
                if isinstance(m, dict) and m.get("drillDownAvailable") and m.get("month")
            }
            da_saltare |= {
                (a, m) for (a, m) in mesi_richiesti if a == anno and m not in drillabili
            }
        return da_saltare

    async def _async_viste_da_excel(
        self, api: SetApiClient, pod: str, anno: int, mese: int
    ) -> dict[date, list[dict[str, Any]]] | None:
        """Curva del mese dall'export Excel (una chiamata), gia' nella forma
        della vista giorno per ogni giorno presente. None se l'Excel non
        e' disponibile o non e' parsabile: il chiamante ricade sulla vista
        giorno. Un mese vuoto (404) e' un dict vuoto, non None: non ha
        senso rifare 30 chiamate a vuoto."""
        fiscal_code, profile = await self._async_identita(api)
        contract_id = await self._async_contract_id(api, pod)
        try:
            try:
                xlsx = await api.async_get_consumption_excel_month(
                    fiscal_code, contract_id, pod, profile, anno, mese
                )
            except SetApiUnauthorized:
                api = await self._async_login(forza=True)
                xlsx = await api.async_get_consumption_excel_month(
                    fiscal_code, contract_id, pod, profile, anno, mese
                )
        except SetApiError as err:
            _LOGGER.warning(
                "POD %s: Excel mensile %d-%02d non disponibile (%s), ricado sulla vista giorno",
                pod, anno, mese, err,
            )
            return None
        if xlsx is None:
            return {}
        try:
            viste = await self.hass.async_add_executor_job(parse_excel_mensile, xlsx)
        except SetExcelError as err:
            _LOGGER.warning(
                "POD %s: Excel mensile %d-%02d non parsabile (%s), ricado sulla vista giorno",
                pod, anno, mese, err,
            )
            return None
        _LOGGER.debug("POD %s: Excel %d-%02d con %d giorni", pod, anno, mese, len(viste))
        return viste

    # ------------------------------------------------------------------
    # Recupero storico manuale (azione contatore_letture.recupera_storico)
    # ------------------------------------------------------------------

    async def async_recupera_storico(
        self, data_da: date, data_a: date, pod: str | None = None
    ) -> None:
        """Recupera e importa la curva per [data_da, data_a]: un export
        Excel per ogni mese toccato (tutta la curva a 15 minuti del mese in
        una chiamata, vedi excel.py), con ripiego alla vista giorno JSON,
        una chiamata per giorno, se l'Excel di un mese non e' utilizzabile.
        I mesi che la vista anno dichiara senza dettaglio vengono saltati.

        Se 'pod' è omesso, lo fa per TUTTI i POD configurati sulla entry;
        se specificato, solo per quello. I giorni recuperati con successo
        vengono anche tolti dalla coda del ciclo automatico, se ci erano
        finiti.

        Solleva HomeAssistantError se al termine non è stato importato
        nessun giorno: l'azione è manuale e lanciata dall'interfaccia,
        dove un fallimento silenzioso è indistinguibile da un successo
        (stesso principio di PCF/E-Distribuzione/Areti/Ireti, issue #4).
        """
        if pod is not None and pod not in self.pods:
            raise ServiceValidationError(
                f"Il POD '{pod}' non è configurato su questa istanza. "
                f"POD configurati: {', '.join(self.pods)}"
            )
        pod_da_recuperare = [pod] if pod else list(self.pods)

        if data_da > data_a:
            raise ServiceValidationError(
                f"La data di inizio ({data_da}) è successiva a quella di fine ({data_a})."
            )

        giorni = _giorni_nel_periodo(data_da, data_a)
        if len(giorni) > MAX_GIORNI_RECUPERO_STORICO:
            raise ServiceValidationError(
                f"Intervallo di {len(giorni)} giorni troppo ampio per una singola "
                f"richiesta (limite di cortesia: {MAX_GIORNI_RECUPERO_STORICO} giorni). "
                "Ripeti l'azione su periodi più corti."
            )

        api = await self._async_login()

        _LOGGER.info(
            "Recupero storico SET avviato: %s - %s (%d giorni, POD: %s)",
            data_da, data_a, len(giorni), ", ".join(pod_da_recuperare),
        )

        fallimenti: list[str] = []
        giorni_importati = 0

        for pod_corrente in pod_da_recuperare:
            trovati = 0
            mesi_da_saltare = await self._async_mesi_senza_dettaglio(api, pod_corrente, giorni)
            if mesi_da_saltare:
                _LOGGER.info(
                    "POD %s: salto i mesi senza dettaglio a 15 minuti (drillDownAvailable "
                    "false o anno assente): %s",
                    pod_corrente, ", ".join(f"{a}-{m:02d}" for a, m in sorted(mesi_da_saltare)),
                )
            mesi = sorted({(g.year, g.month) for g in giorni} - mesi_da_saltare)
            for anno, mese in mesi:
                giorni_del_mese = [g for g in giorni if (g.year, g.month) == (anno, mese)]
                # 1. Excel mensile: tutta la curva del mese in una chiamata.
                viste = await self._async_viste_da_excel(api, pod_corrente, anno, mese)
                # 2. Ripiego giorno per giorno solo se l'Excel non e' utilizzabile.
                if viste is None:
                    viste = {}
                    for giorno in giorni_del_mese:
                        try:
                            api, vista = await self._async_vista_giorno(api, pod_corrente, giorno)
                        except (SetApiError, SetAuthError) as err:
                            _LOGGER.warning(
                                "POD %s: errore recuperando il giorno %s: %s",
                                pod_corrente, giorno, err,
                            )
                            fallimenti.append(f"{pod_corrente}/{giorno}: {err}")
                            continue
                        if giorno_pubblicato(vista):
                            viste[giorno] = vista
                for giorno in giorni_del_mese:
                    vista = viste.get(giorno)
                    if not vista or not giorno_pubblicato(vista):
                        continue
                    await async_import_curva_giorno(self.hass, pod_corrente, giorno, vista)
                    self._rimuovi_dalla_coda(pod_corrente, [giorno])
                    trovati += 1
                    giorni_importati += 1

            _LOGGER.info(
                "POD %s: recupero storico completato, %d giorni trovati nel periodo %s - %s",
                pod_corrente, trovati, data_da, data_a,
            )
            if trovati == 0:
                fallimenti.append(f"{pod_corrente}: nessun giorno disponibile nel periodo richiesto")

        if giorni_importati == 0:
            raise HomeAssistantError(
                f"Nessun dato importato per il periodo {data_da} - {data_a}. "
                + "; ".join(fallimenti)
            )
