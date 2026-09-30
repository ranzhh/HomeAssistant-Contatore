"""Coda dei giorni da riprovare del SetCoordinator (per-POD).

Stesso meccanismo di edistribuzione/ireti (abbandono a tempo, non a
conteggio tentativi) - vedi tests/ireti/test_coordinator_coda.py. La coda
e' nuova con questo distributore, quindi nessun test di migrazione da
formati precedenti.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from freezegun import freeze_time

from custom_components.contatore_letture.distributors.set.const import (
    ABBANDONO_CODA_DOPO_GIORNI,
    CONF_GIORNI_DA_RIPROVARE,
    MAX_GIORNI_IN_CODA,
)

from .conftest import POD_A, POD_B

OGGI = "2026-09-15 12:00:00"


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    await hass.config.async_set_time_zone("UTC")


async def test_abbandona_i_giorni_troppo_vecchi_per_pod(hass, make_set_coordinator):
    vecchio = (date(2026, 9, 15) - timedelta(days=ABBANDONO_CODA_DOPO_GIORNI)).isoformat()
    recente = "2026-09-14"
    coordinator = make_set_coordinator(
        pods=[POD_A],
        data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {"2026-08-20": vecchio, "2026-09-12": recente}}},
    )

    with freeze_time(OGGI):
        coordinator._accoda_giorno(POD_A, date(2026, 9, 13))
        code = coordinator._leggi_code()

    assert set(code[POD_A]) == {"2026-09-12", "2026-09-13"}


async def test_accodare_due_volte_lo_stesso_giorno_non_ne_resetta_il_timer(
    hass, make_set_coordinator
):
    coordinator = make_set_coordinator(pods=[POD_A])

    with freeze_time("2026-09-10 12:00:00"):
        coordinator._accoda_giorno(POD_A, date(2026, 9, 9))

    with freeze_time("2026-09-16 12:00:00"):
        coordinator._accoda_giorno(POD_A, date(2026, 9, 9))
        code = coordinator._leggi_code()

    assert code[POD_A]["2026-09-09"] == date(2026, 9, 10)


async def test_le_code_dei_pod_sono_indipendenti(hass, make_set_coordinator):
    coordinator = make_set_coordinator(pods=[POD_A, POD_B])

    with freeze_time(OGGI):
        coordinator._accoda_giorno(POD_A, date(2026, 9, 13))
        coordinator._accoda_giorno(POD_B, date(2026, 9, 12))
        coordinator._rimuovi_dalla_coda(POD_A, [date(2026, 9, 13)])
        code = coordinator._leggi_code()

    assert POD_A not in code
    assert set(code[POD_B]) == {"2026-09-12"}


async def test_rimuovere_un_giorno_non_in_coda_non_fa_nulla(hass, make_set_coordinator):
    coordinator = make_set_coordinator(pods=[POD_A])
    with freeze_time(OGGI):
        coordinator._rimuovi_dalla_coda(POD_A, [date(2026, 9, 1)])
    assert CONF_GIORNI_DA_RIPROVARE not in coordinator.entry.data


async def test_tetto_alla_coda_scarta_i_piu_vecchi(hass, make_set_coordinator):
    coordinator = make_set_coordinator(pods=[POD_A])
    with freeze_time(OGGI):
        for i in range(MAX_GIORNI_IN_CODA + 5):
            coordinator._accoda_giorno(POD_A, date(2026, 9, 14) - timedelta(days=i))
        code = coordinator._leggi_code()

    assert len(code[POD_A]) == MAX_GIORNI_IN_CODA
    assert max(code[POD_A]) == "2026-09-14"
