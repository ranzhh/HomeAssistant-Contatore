"""Recupero storico e ciclo automatico contro il recorder vero, non un mock."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.contatore_letture.distributors.set.const import CONF_GIORNI_DA_RIPROVARE
from custom_components.contatore_letture.statistics_common import sanitize_statistic_id

from .conftest import POD_A
from .test_coordinator import _api, _vista_giorno

GIORNI = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Il recorder va avviato prima di hass."""
    yield


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    await hass.config.async_set_time_zone("UTC")


async def _somme(hass) -> list[float]:
    await async_wait_recording_done(hass)
    statistic_id = sanitize_statistic_id(POD_A)
    righe = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return [r["sum"] for r in righe[statistic_id]]


# Database su file come in produzione: in memoria le letture si accodano
# alle scritture del recorder e la somma che riparte da zero non si vede.
@pytest.mark.parametrize("persistent_database", [True])
async def test_recupero_storico_continua_la_somma(hass, make_set_coordinator):
    coordinator = make_set_coordinator()
    coordinator._async_login = AsyncMock(
        return_value=_api({g: _vista_giorno(g) for g in GIORNI})
    )

    await coordinator.async_recupera_storico(GIORNI[0], GIORNI[-1])

    somme = await _somme(hass)
    assert len(somme) == 72
    assert somme == sorted(somme)
    assert somme[-1] == pytest.approx(72 * 0.4)


@pytest.mark.parametrize("persistent_database", [True])
async def test_ciclo_che_recupera_arretrati_continua_la_somma(hass, make_set_coordinator):
    # Il caso di leras: un giorno in coda e il login fermo per giorni.
    coordinator = make_set_coordinator(
        data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {GIORNI[0].isoformat(): "2026-09-02"}}}
    )
    coordinator._async_login = AsyncMock(
        return_value=_api({g: _vista_giorno(g) for g in GIORNI})
    )

    with freeze_time("2026-09-04 20:00:00"):
        await coordinator._async_update_data()

    somme = await _somme(hass)
    assert len(somme) == 72
    assert somme == sorted(somme)
