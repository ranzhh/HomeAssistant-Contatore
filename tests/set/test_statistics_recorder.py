"""async_import_curva_giorno contro il recorder vero, non un mock."""
from __future__ import annotations

from datetime import UTC, date

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.contatore_letture.distributors.set import statistics as st
from custom_components.contatore_letture.statistics_common import sanitize_statistic_id

POD = "IT001E00000000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Il recorder va avviato prima di hass."""
    yield


@pytest.fixture(autouse=True)
def _fuso_utc():
    originale = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(UTC)
    yield
    dt_util.set_default_time_zone(originale)


def _giorno(giorno: date) -> list[dict]:
    return [
        {
            "year": giorno.year,
            "month": giorno.month,
            "day": giorno.day,
            "hour": h,
            "estimated": False,
            "kConstant": "1",
            "quarters": [0.1] * 4,
            "total": 0.4,
        }
        for h in range(24)
    ]


# Database su file come in produzione: in memoria le letture si accodano
# alle scritture del recorder e il problema non si vede.
@pytest.mark.parametrize("persistent_database", [True])
async def test_giorni_consecutivi_continuano_la_somma(hass):
    # Il recupero storico importa un giorno dopo l'altro senza pause.
    for giorno in (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)):
        await st.async_import_curva_giorno(hass, POD, giorno, _giorno(giorno))
    await async_wait_recording_done(hass)

    statistic_id = sanitize_statistic_id(POD)
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
    somme = [r["sum"] for r in righe[statistic_id]]

    assert len(somme) == 72
    assert somme == sorted(somme)
    assert somme[-1] == pytest.approx(72 * 0.4)
