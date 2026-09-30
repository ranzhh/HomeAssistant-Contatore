"""Test della logica di aggregazione oraria (distributors/set/statistics.py).

Coperte solo le funzioni pure (_aggrega_per_ora, _kwh_dell_ora,
kwh_del_giorno): il percorso completo async_import_curva_giorno richiede
il recorder ed e' fuori da questi test (stesso approccio di
tests/ireti/test_statistics.py).

Il fuso di riferimento e' fissato a UTC, quindi locale == UTC: il cambio
ora non viene simulato (non e' nemmeno verificato come lo gestisca il
portale, vedi statistics.py).
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from homeassistant.util import dt as dt_util

from custom_components.contatore_letture.distributors.set import statistics as st

GIORNO = date(2026, 9, 20)


@pytest.fixture(autouse=True)
def _fuso_utc():
    originale = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(UTC)
    yield
    dt_util.set_default_time_zone(originale)


def _ora(hour: int, quarters=None, total=None, giorno: date = GIORNO, **extra) -> dict:
    elemento = {
        "drillDownAvailable": True,
        "year": giorno.year,
        "month": giorno.month,
        "day": giorno.day,
        "hour": hour,
        "estimated": False,
        "meterSerialNumber": "matricola-di-fantasia",
        "kConstant": "1",
        **extra,
    }
    if quarters is not None:
        elemento["quarters"] = quarters
        elemento["total"] = round(sum(q for q in quarters if q is not None), 3)
    if total is not None:
        elemento["total"] = total
    return elemento


def _giorno_completo(valore_quarto: float = 0.1) -> list[dict]:
    return [_ora(h, [valore_quarto] * 4) for h in range(24)]


# --- _kwh_dell_ora -----------------------------------------------------------

def test_somma_dei_quarti():
    assert st._kwh_dell_ora(_ora(0, [0.117, 0.124, 0.289, 0.046])) == pytest.approx(0.576)


def test_quarti_con_null_vengono_saltati():
    assert st._kwh_dell_ora(_ora(0, [0.1, None, 0.1, 0.1])) == pytest.approx(0.3)


def test_senza_quarti_usa_total():
    assert st._kwh_dell_ora(_ora(0, total=1.5)) == pytest.approx(1.5)


def test_senza_nulla_di_numerico_ritorna_none():
    assert st._kwh_dell_ora({"hour": 0}) is None


# --- _aggrega_per_ora ---------------------------------------------------------

def test_un_bucket_per_ora_in_utc():
    risultato = dict(st._aggrega_per_ora(GIORNO, _giorno_completo(0.25)))
    assert len(risultato) == 24
    assert risultato[datetime(2026, 9, 20, 0, 0, tzinfo=UTC)] == pytest.approx(1.0)
    assert risultato[datetime(2026, 9, 20, 23, 0, tzinfo=UTC)] == pytest.approx(1.0)


def test_risultato_ordinato_per_ora():
    ore = [_ora(5, [0.1] * 4), _ora(2, [0.1] * 4)]
    ts = [t for t, _ in st._aggrega_per_ora(GIORNO, ore)]
    assert ts == sorted(ts)


def test_elemento_di_un_altro_giorno_viene_scartato():
    ore = [_ora(0, [0.1] * 4), _ora(1, [0.1] * 4, giorno=date(2026, 9, 21))]
    risultato = dict(st._aggrega_per_ora(GIORNO, ore))
    assert list(risultato) == [datetime(2026, 9, 20, 0, 0, tzinfo=UTC)]


def test_ora_fuori_range_viene_scartata():
    ore = [_ora(0, [0.1] * 4), _ora(24, [0.1] * 4)]
    assert len(st._aggrega_per_ora(GIORNO, ore)) == 1


def test_elemento_senza_campi_obbligatori_viene_scartato():
    ore = [{"quarters": [0.1] * 4}, _ora(3, [0.1] * 4)]
    risultato = dict(st._aggrega_per_ora(GIORNO, ore))
    assert list(risultato) == [datetime(2026, 9, 20, 3, 0, tzinfo=UTC)]


def test_kconstant_diversa_da_uno_non_moltiplica(caplog):
    """Mai osservato: si importa cosi' com'e' e si avvisa nei log."""
    ore = [_ora(0, [0.1] * 4, kConstant="40")]
    risultato = dict(st._aggrega_per_ora(GIORNO, ore))
    assert risultato[datetime(2026, 9, 20, 0, 0, tzinfo=UTC)] == pytest.approx(0.4)
    assert "kConstant" in caplog.text


def test_risposta_vuota_da_lista_vuota():
    assert st._aggrega_per_ora(GIORNO, []) == []


# --- kwh_del_giorno -------------------------------------------------------------

def test_kwh_del_giorno_somma_le_ore():
    assert st.kwh_del_giorno(_giorno_completo(0.1)) == pytest.approx(9.6)


def test_kwh_del_giorno_none_se_vuoto():
    assert st.kwh_del_giorno([]) is None


# --- statistic id (condiviso, ma verifichiamo il formato usato qui) -------------

def test_statistic_id_dal_pod():
    assert st.sanitize_statistic_id("IT221E00000000001") == "contatore_letture:it221e00000000001_energia"
