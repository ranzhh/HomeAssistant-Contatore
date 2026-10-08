"""Test per distributors/set/coordinator.py: ciclo automatico (coda dei
giorni da riprovare, orario di cortesia, cache dei token,
ConfigEntryAuthFailed) ed esiti di recupera_storico (stesso contratto di
PCF/E-Distribuzione/Areti/Ireti - issue #4: un fallimento silenzioso deve
diventare un errore visibile)."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest
from freezegun import freeze_time
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.contatore_letture.distributors.set.coordinator as mod
from custom_components.contatore_letture.distributors.set.api import SetApiError
from custom_components.contatore_letture.distributors.set.auth import (
    SetInvalidCredentials,
    SetTokens,
)
from custom_components.contatore_letture.distributors.set.const import (
    CONF_CONTRACT_IDS,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_ORA_RICHIESTA,
)

from .conftest import CF, CONTRACT_A, POD_A, PROFILE

DATA_DA = date(2026, 7, 1)
DATA_A = date(2026, 7, 31)

TOKENS = SetTokens(
    access_token="access-di-fantasia",
    refresh_token="refresh-di-fantasia",
    expires_in=3600,
    refresh_token_expires_in=86400,
)


def _ora(giorno: date, hour: int, quarters=(0.1, 0.1, 0.1, 0.1)) -> dict:
    return {
        "drillDownAvailable": True,
        "year": giorno.year, "month": giorno.month, "day": giorno.day, "hour": hour,
        "quarters": list(quarters), "total": round(sum(quarters), 3),
        "average": round(sum(quarters) / 4, 5), "estimated": False,
        "meterSerialNumber": "matricola-di-fantasia", "kConstant": "1",
    }


def _vista_giorno(giorno: date) -> list[dict]:
    return [_ora(giorno, h) for h in range(24)]


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    await hass.config.async_set_time_zone("UTC")


@pytest.fixture
async def coordinator(make_set_coordinator):
    return make_set_coordinator()


@pytest.fixture
def _no_statistiche(monkeypatch):
    """Nessun recorder nei test: import e lettura statistiche sostituiti."""
    importa = AsyncMock(return_value=None)
    monkeypatch.setattr(mod, "async_import_curve", importa)
    monkeypatch.setattr(mod, "async_get_ultima_data_disponibile", AsyncMock(return_value=None))
    return importa


def _importati(importa) -> list[date]:
    """Giorni passati all'import, su tutte le chiamate."""
    return sorted(g for c in importa.await_args_list for g in c.args[2])


def _api(viste: dict[date, list] | None = None, errore=None):
    api = Mock()

    async def _giorno(fiscal_code, contract_id, pod, profile, giorno):
        assert (fiscal_code, contract_id, pod, profile) == (CF, CONTRACT_A, POD_A, PROFILE)
        if errore is not None:
            raise errore
        return (viste or {}).get(giorno, [])

    api.async_get_consumption_day = AsyncMock(side_effect=_giorno)
    # Excel mensile: di default NON disponibile (errore), cosi' i test del
    # recupero storico esercitano il ripiego giorno per giorno; i test
    # dedicati all'Excel lo sovrascrivono.
    api.async_get_consumption_excel_month = AsyncMock(side_effect=SetApiError("excel ko"))
    # vista anno: di default tutti i mesi drillabili
    api.async_get_consumption_year = AsyncMock(
        side_effect=lambda fc, cid, pod, prof, anno: [
            {"year": anno, "month": m, "drillDownAvailable": True, "total": 1.0} for m in range(1, 13)
        ]
    )
    return api


def _segnaposto(giorno: date) -> list[dict]:
    """Forma reale di un giorno non ancora pubblicato (30/09/2026)."""
    return [
        {"year": giorno.year, "month": giorno.month, "day": giorno.day, "hour": h,
         "total": 0.0, "estimated": True}
        for h in range(24)
    ]


class TestLogin:
    async def test_credenziali_invalide_solleva_configentryauthfailed(self, coordinator, monkeypatch):
        monkeypatch.setattr(
            coordinator._auth, "async_login",
            AsyncMock(side_effect=SetInvalidCredentials("rifiutato")),
        )
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_login()

    async def test_token_in_cache_evita_un_secondo_login(self, coordinator, monkeypatch):
        login = AsyncMock(return_value=TOKENS)
        monkeypatch.setattr(coordinator._auth, "async_login", login)
        await coordinator._async_login()
        await coordinator._async_login()
        login.assert_awaited_once()

    async def test_access_scaduto_usa_il_refresh(self, coordinator, monkeypatch):
        login = AsyncMock(return_value=TOKENS)
        refresh = AsyncMock(return_value=TOKENS)
        monkeypatch.setattr(coordinator._auth, "async_login", login)
        monkeypatch.setattr(coordinator._auth, "async_refresh", refresh)
        await coordinator._async_login()
        coordinator._access_scade_a = 0.0  # simulo l'ora scaduta
        await coordinator._async_login()
        login.assert_awaited_once()
        refresh.assert_awaited_once_with("refresh-di-fantasia")

    async def test_refresh_fallito_ricade_sul_login(self, coordinator, monkeypatch):
        from custom_components.contatore_letture.distributors.set.auth import SetAuthError

        login = AsyncMock(return_value=TOKENS)
        monkeypatch.setattr(coordinator._auth, "async_login", login)
        monkeypatch.setattr(coordinator._auth, "async_refresh", AsyncMock(side_effect=SetAuthError("no")))
        await coordinator._async_login()
        coordinator._access_scade_a = 0.0
        await coordinator._async_login()
        assert login.await_count == 2


class TestProssimaRichiesta:
    async def test_dopo_l_orario_di_cortesia_chiede_ieri(self, coordinator, _no_statistiche):
        with freeze_time("2026-09-18 20:00:00"):
            assert await coordinator._prossima_richiesta(POD_A) == [date(2026, 9, 17)]

    async def test_prima_dell_orario_di_cortesia_chiede_l_altro_ieri(self, coordinator, _no_statistiche):
        with freeze_time("2026-09-18 09:00:00"):
            assert await coordinator._prossima_richiesta(POD_A) == [date(2026, 9, 16)]

    async def test_orario_configurabile_dalle_opzioni(self, make_set_coordinator, _no_statistiche):
        coordinator = make_set_coordinator(options={CONF_ORA_RICHIESTA: 8})
        with freeze_time("2026-09-18 09:00:00"):
            assert await coordinator._prossima_richiesta(POD_A) == [date(2026, 9, 17)]

    async def test_niente_da_fare_se_gia_importato(self, coordinator, monkeypatch, _no_statistiche):
        monkeypatch.setattr(
            mod, "async_get_ultima_data_disponibile", AsyncMock(return_value=date(2026, 9, 17))
        )
        with freeze_time("2026-09-18 20:00:00"):
            assert await coordinator._prossima_richiesta(POD_A) == []

    async def test_arretrati_in_coda_vengono_richiesti_uno_per_uno(
        self, hass, coordinator, monkeypatch, _no_statistiche
    ):
        monkeypatch.setattr(
            mod, "async_get_ultima_data_disponibile", AsyncMock(return_value=date(2026, 9, 16))
        )
        hass.config_entries.async_update_entry(
            coordinator.entry,
            data={**coordinator.entry.data,
                  CONF_GIORNI_DA_RIPROVARE: {POD_A: {"2026-09-14": "2026-09-15"}}},
        )
        with freeze_time("2026-09-18 20:00:00"):
            giorni = await coordinator._prossima_richiesta(POD_A)
        # il 14 (in coda) e il 17 (atteso, non ancora importato); il 15 e il
        # 16 no: gia' coperti dalle statistiche e non in coda
        assert giorni == [date(2026, 9, 14), date(2026, 9, 17)]


class TestUpdateData:
    async def test_giorno_presente_viene_importato_e_non_va_in_coda(
        self, coordinator, _no_statistiche
    ):
        atteso = date(2026, 9, 17)
        coordinator._async_login = AsyncMock(return_value=_api({atteso: _vista_giorno(atteso)}))

        with freeze_time("2026-09-18 20:00:00"):
            dati = await coordinator._async_update_data()
            code = coordinator._leggi_code()

        assert dati["by_pod"][POD_A]["ultimo_giorno_importato"] == "2026-09-17"
        assert dati["by_pod"][POD_A]["kwh_ultimo_giorno_importato"] == pytest.approx(9.6)
        assert "2026-09-17" not in code.get(POD_A, {})
        assert _importati(_no_statistiche) == [atteso]

    async def test_giorno_mancante_va_in_coda(self, coordinator, _no_statistiche):
        coordinator._async_login = AsyncMock(return_value=_api({}))

        with freeze_time("2026-09-18 20:00:00"):
            dati = await coordinator._async_update_data()
            code = coordinator._leggi_code()

        assert dati["by_pod"][POD_A]["ultimo_giorno_importato"] is None
        assert set(code[POD_A]) == {"2026-09-17"}
        _no_statistiche.assert_not_awaited()

    async def test_giorno_con_soli_segnaposto_va_in_coda_e_non_viene_importato(
        self, coordinator, _no_statistiche
    ):
        """Il caso reale del giorno corrente: 24 elementi estimated senza
        quarters. Importarli scriverebbe 24 ore a zero nelle statistiche e
        il giorno uscirebbe dalla coda senza essere mai riprovato."""
        atteso = date(2026, 9, 17)
        coordinator._async_login = AsyncMock(return_value=_api({atteso: _segnaposto(atteso)}))

        with freeze_time("2026-09-18 20:00:00"):
            dati = await coordinator._async_update_data()
            code = coordinator._leggi_code()

        assert dati["by_pod"][POD_A]["ultimo_giorno_importato"] is None
        assert set(code[POD_A]) == {"2026-09-17"}
        _no_statistiche.assert_not_awaited()

    async def test_senza_nulla_da_chiedere_non_fa_login(self, coordinator, monkeypatch, _no_statistiche):
        monkeypatch.setattr(
            mod, "async_get_ultima_data_disponibile", AsyncMock(return_value=date(2026, 9, 17))
        )
        login = AsyncMock()
        coordinator._async_login = login

        with freeze_time("2026-09-18 20:00:00"):
            dati = await coordinator._async_update_data()

        login.assert_not_awaited()
        assert dati["by_pod"][POD_A]["ultima_data_disponibile"] == "2026-09-17"

    async def test_errore_api_solleva_updatefailed_e_accoda(self, coordinator, _no_statistiche):
        coordinator._async_login = AsyncMock(return_value=_api(errore=SetApiError("500 dal portale")))

        with freeze_time("2026-09-18 20:00:00"), pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

        with freeze_time("2026-09-18 20:00:00"):
            code = coordinator._leggi_code()
        assert set(code[POD_A]) == {"2026-09-17"}

    async def test_401_rifa_login_una_volta(self, coordinator, _no_statistiche):
        from custom_components.contatore_letture.distributors.set.api import SetApiUnauthorized

        atteso = date(2026, 9, 17)
        api_scaduto = _api(errore=SetApiUnauthorized("401"))
        api_buono = _api({atteso: _vista_giorno(atteso)})
        coordinator._async_login = AsyncMock(side_effect=[api_scaduto, api_buono])

        with freeze_time("2026-09-18 20:00:00"):
            dati = await coordinator._async_update_data()

        assert dati["by_pod"][POD_A]["ultimo_giorno_importato"] == "2026-09-17"
        assert coordinator._async_login.await_count == 2

    async def test_contract_id_mancante_viene_risolto_e_persistito(
        self, hass, make_set_coordinator, _no_statistiche
    ):
        coordinator = make_set_coordinator(data={CONF_CONTRACT_IDS: {}})
        atteso = date(2026, 9, 17)
        api = _api({atteso: _vista_giorno(atteso)})
        api.async_get_active_utilities = AsyncMock(
            return_value=[{"podPdr": POD_A, "contractId": CONTRACT_A, "utilityType": "ENERGY"}]
        )
        coordinator._async_login = AsyncMock(return_value=api)

        with freeze_time("2026-09-18 20:00:00"):
            await coordinator._async_update_data()

        api.async_get_active_utilities.assert_awaited_once_with(CF, PROFILE)
        assert coordinator.entry.data[CONF_CONTRACT_IDS] == {POD_A: CONTRACT_A}


class TestRecuperaStorico:
    @pytest.fixture(autouse=True)
    def _login(self, coordinator):
        self._api = _api()
        coordinator._async_login = AsyncMock(return_value=self._api)

    async def test_pod_non_configurato_solleva_servicevalidationerror(self, coordinator):
        with pytest.raises(ServiceValidationError):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A, pod="IT999X99999999")

    async def test_data_da_dopo_data_a_solleva_servicevalidationerror(self, coordinator):
        with pytest.raises(ServiceValidationError):
            await coordinator.async_recupera_storico(DATA_A, DATA_DA)

    async def test_intervallo_troppo_ampio_solleva_servicevalidationerror(self, coordinator):
        with pytest.raises(ServiceValidationError, match="troppo ampio"):
            await coordinator.async_recupera_storico(date(2023, 1, 1), date(2026, 1, 1))

    async def test_errore_api_fa_fallire_l_azione(self, coordinator, _no_statistiche):
        coordinator._async_login = AsyncMock(return_value=_api(errore=SetApiError("500 dal portale")))
        with pytest.raises(HomeAssistantError, match="500 dal portale"):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A)

    async def test_nessun_giorno_disponibile_fa_fallire_l_azione(self, coordinator, _no_statistiche):
        with pytest.raises(HomeAssistantError, match="[Nn]essun"):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A)

    async def test_salta_i_mesi_senza_dettaglio(self, coordinator, _no_statistiche):
        """Vista anno con drillDownAvailable false per luglio: i 31 giorni
        di luglio non vengono nemmeno chiesti; agosto si."""
        viste = {date(2026, 8, 1): _vista_giorno(date(2026, 8, 1))}
        api = _api(viste)
        api.async_get_consumption_year = AsyncMock(return_value=[
            {"year": 2026, "month": 7, "drillDownAvailable": False, "total": 100.0},
            {"year": 2026, "month": 8, "drillDownAvailable": True, "total": 100.0},
        ])
        coordinator._async_login = AsyncMock(return_value=api)

        await coordinator.async_recupera_storico(date(2026, 7, 1), date(2026, 8, 2))

        giorni_chiesti = [c.args[4] for c in api.async_get_consumption_day.await_args_list]
        assert giorni_chiesti == [date(2026, 8, 1), date(2026, 8, 2)]
        api.async_get_consumption_year.assert_awaited_once()

    async def test_vista_anno_in_errore_non_salta_nulla(self, coordinator, _no_statistiche):
        viste = {date(2026, 7, 1): _vista_giorno(date(2026, 7, 1))}
        api = _api(viste)
        api.async_get_consumption_year = AsyncMock(side_effect=SetApiError("500"))
        coordinator._async_login = AsyncMock(return_value=api)

        await coordinator.async_recupera_storico(date(2026, 7, 1), date(2026, 7, 2))

        assert api.async_get_consumption_day.await_count == 2

    async def test_excel_mensile_evita_le_chiamate_per_giorno(
        self, coordinator, _no_statistiche, monkeypatch
    ):
        """Percorso principale: un Excel per mese, nessuna vista giorno."""
        g1, g2 = date(2026, 7, 1), date(2026, 7, 2)
        api = _api()
        api.async_get_consumption_excel_month = AsyncMock(return_value=b"xlsx-di-fantasia")
        monkeypatch.setattr(
            mod, "parse_excel_mensile",
            lambda xlsx: {g1: _vista_giorno(g1), g2: _vista_giorno(g2)},
        )
        coordinator._async_login = AsyncMock(return_value=api)

        await coordinator.async_recupera_storico(g1, date(2026, 7, 3))

        api.async_get_consumption_excel_month.assert_awaited_once_with(
            CF, CONTRACT_A, POD_A, PROFILE, 2026, 7
        )
        api.async_get_consumption_day.assert_not_awaited()
        assert _importati(_no_statistiche) == [g1, g2]  # il 3 luglio non e' nel file

    async def test_excel_assente_404_non_ricade_sulla_vista_giorno(
        self, coordinator, _no_statistiche
    ):
        """Mese senza file (404 -> None): e' un mese vuoto, non serve
        rifare 30 chiamate; l'azione fallisce come 'nessun dato'."""
        api = _api()
        api.async_get_consumption_excel_month = AsyncMock(return_value=None)
        coordinator._async_login = AsyncMock(return_value=api)

        with pytest.raises(HomeAssistantError, match="[Nn]essun"):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A)

        api.async_get_consumption_day.assert_not_awaited()

    async def test_excel_non_parsabile_ricade_sulla_vista_giorno(
        self, coordinator, _no_statistiche, monkeypatch
    ):
        from custom_components.contatore_letture.distributors.set.excel import SetExcelError

        g = date(2026, 7, 1)
        api = _api({g: _vista_giorno(g)})
        api.async_get_consumption_excel_month = AsyncMock(return_value=b"rotto")

        def _rotto(xlsx):
            raise SetExcelError("layout cambiato")

        monkeypatch.setattr(mod, "parse_excel_mensile", _rotto)
        coordinator._async_login = AsyncMock(return_value=api)

        await coordinator.async_recupera_storico(g, date(2026, 7, 2))

        assert api.async_get_consumption_day.await_count == 2
        assert _importati(_no_statistiche) == [g]

    async def test_successo_importa_i_giorni_pubblicati(self, coordinator, _no_statistiche):
        viste = {g: _vista_giorno(g) for g in (date(2026, 7, 1), date(2026, 7, 2))}
        api = _api(viste)
        coordinator._async_login = AsyncMock(return_value=api)

        await coordinator.async_recupera_storico(date(2026, 7, 1), date(2026, 7, 3))

        assert api.async_get_consumption_day.await_count == 3
        assert _importati(_no_statistiche) == sorted(viste)
