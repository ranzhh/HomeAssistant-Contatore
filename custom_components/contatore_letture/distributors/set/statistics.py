"""Import della curva di carico SET come external statistics.

Schema di un elemento di `consumptions` nella vista "giorno" (risposta di
SetApiClient.async_get_consumption_day), CONFERMATO su dati reali il
30/09/2026:

    {
      "drillDownAvailable": true,
      "year": 2026, "month": 9, "day": 20, "hour": 13,
      "quarters": [0.117, 0.124, 0.289, 0.046],
      "total": 0.576,
      "average": 0.144,
      "estimated": false,
      "meterSerialNumber": "...",
      "kConstant": "1"
    }

quarters: 4 valori per ora, indice 0 = hh:00-hh:15 locale, in kWh (unità
confermata dall'export Excel dello stesso portale - "Unità di misura: kWh"
- e dal confronto dei totali mensili con il contatore fisico fatto da chi
ha fornito la cattura). `total` è la somma dei quattro quarti (verificato
su tutte le 24 ore della cattura). Qui si sommano i quarti (fonte più
fine) e si usa `total` solo se `quarters` manca.

kConstant: "1" in tutti i dati osservati. Non è noto se, con una costante
diversa da 1 (contatori con TA, utenze grandi), i valori arrivino già
moltiplicati o no: si logga un avviso e NON si moltiplica.

estimated: false ovunque nella cattura. I valori stimati vengono
importati comunque (l'import è idempotente: quando arriva il dato reale
sostituisce quello stimato per la stessa ora).

Fuso orario: gli orari sono locali (Europe/Rome per una fornitura SET) e
la risposta non porta nessun offset: si ricostruisce l'ora locale da
(year, month, day, hour) e si converte in UTC con dt_util.as_utc, come
per Ireti/Areti - è il database dei fusi orari di Home Assistant a
gestire il cambio ora.

NON VERIFICATO: comportamento nei giorni di cambio ora legale (23 o 25
elementi? un `hour` ripetuto?). La cattura (settembre 2026) non ne
conteneva nessuno. Un `hour` fuori da 0-23 viene scartato con un avviso;
un `hour` duplicato (25 ore) finirebbe sommato nello stesso bucket UTC
solo se la conversione locale->UTC lo distinguesse, cosa che senza
l'attributo fold non è garantita - da verificare a fine ottobre.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ...statistics_common import (
    async_scrivi_serie_oraria,
    async_ultima_data_disponibile,
    sanitize_statistic_id,
)

_LOGGER = logging.getLogger(__name__)

_QUARTI_ATTESI = 4


def _kwh_dell_ora(elemento: dict[str, Any]) -> float | None:
    """Somma dei `quarters` (o `total` in mancanza), None se non c'è nulla
    di numerico."""
    quarti = elemento.get("quarters")
    if isinstance(quarti, list) and quarti:
        if len(quarti) != _QUARTI_ATTESI:
            _LOGGER.warning(
                "Ora %s del %s/%s/%s con %d quarti d'ora (attesi %d) - schema cambiato?",
                elemento.get("hour"), elemento.get("day"), elemento.get("month"),
                elemento.get("year"), len(quarti), _QUARTI_ATTESI,
            )
        somma = 0.0
        trovato = False
        for v in quarti:
            if v is None:
                continue
            try:
                somma += float(v)
                trovato = True
            except (TypeError, ValueError):
                continue
        if trovato:
            return somma
    totale = elemento.get("total")
    if totale is None:
        return None
    try:
        return float(totale)
    except (TypeError, ValueError):
        return None


def _aggrega_per_ora(
    giorno: date, consumptions: list[dict[str, Any]]
) -> list[tuple[datetime, float]]:
    """Elementi orari (locali) della vista giorno -> bucket orari (UTC).

    `giorno` è quello richiesto: si usa per il timestamp e si controlla
    che gli elementi lo confermino (year/month/day), scartando con un log
    quelli di un altro giorno (non dovrebbe succedere; se succede è meglio
    perdere quel dato che scriverlo sotto la data sbagliata)."""
    bucket: dict[datetime, float] = defaultdict(float)
    k_avvisata = False

    for elemento in consumptions:
        try:
            ora = int(elemento["hour"])
            y, m, d = int(elemento["year"]), int(elemento["month"]), int(elemento["day"])
        except (KeyError, TypeError, ValueError) as exc:
            _LOGGER.warning("Elemento orario non valido, scartato: %r (%s)", elemento, exc)
            continue
        if (y, m, d) != (giorno.year, giorno.month, giorno.day):
            _LOGGER.warning(
                "Elemento del %04d-%02d-%02d in una risposta richiesta per %s: scartato",
                y, m, d, giorno,
            )
            continue
        if not 0 <= ora <= 23:
            _LOGGER.warning("Ora %d fuori da 0-23 nel giorno %s: scartata", ora, giorno)
            continue

        k = str(elemento.get("kConstant", "1")).strip().replace(",", ".")
        if k not in ("1", "1.0") and not k_avvisata:
            _LOGGER.warning(
                "POD con kConstant=%r (mai osservato diverso da 1): i valori vengono "
                "importati così come arrivano, senza moltiplicare. Se il totale non torna "
                "col contatore, apri una issue.",
                elemento.get("kConstant"),
            )
            k_avvisata = True

        if elemento.get("estimated") and not elemento.get("quarters"):
            # Segnaposto di un'ora non ancora pubblicata (total 0.0,
            # estimated true, niente quarters - verificato sul giorno
            # corrente): non e' un dato, non va scritto come zero.
            continue

        kwh = _kwh_dell_ora(elemento)
        if kwh is None:
            continue

        locale = datetime.combine(giorno, time(hour=ora)).replace(
            tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
        bucket[dt_util.as_utc(locale)] += kwh

    return sorted(bucket.items())


async def async_import_curve(
    hass: HomeAssistant, pod: str, viste: dict[date, list[dict[str, Any]]]
) -> date | None:
    """Importa le viste giorno di un POD come external statistics (bucket
    orari), con la stessa fusione/ricalcolo cumulativo di tutti gli altri
    distributori (statistics_common), in UNA scrittura sola.

    La scrittura del recorder e' solo accodata: una seconda fusione subito
    dopo rileggerebbe la serie senza i giorni appena scritti e ricomincerebbe
    la somma progressiva da zero. Per questo tutti i giorni di un ciclo (o di
    un recupero storico) passano di qui insieme. Restituisce la data locale
    dell'ultimo punto della serie risultante, o None se non c'era nulla da
    importare."""
    nuove_ore: dict[datetime, float] = {}
    for giorno, consumptions in sorted(viste.items()):
        ore = dict(_aggrega_per_ora(giorno, consumptions))
        if not ore:
            _LOGGER.warning(
                "POD %s, giorno %s: nessun campione valido nella risposta (schema cambiato "
                "rispetto a quello confermato il 30/09/2026?). Risposta grezza: %r",
                pod, giorno, consumptions,
            )
        nuove_ore.update(ore)

    if not nuove_ore:
        return None

    return await async_scrivi_serie_oraria(
        hass, pod, sanitize_statistic_id(pod), f"SET Distribuzione {pod}", nuove_ore
    )


def kwh_del_giorno(consumptions: list[dict[str, Any]]) -> float | None:
    """Totale kWh di una vista giorno (somma delle ore), per il sensore
    diagnostico. None se non c'è nessun valore numerico."""
    valori = [_kwh_dell_ora(e) for e in consumptions if isinstance(e, dict)]
    valori = [v for v in valori if v is not None]
    return sum(valori) if valori else None


async def async_get_ultima_data_disponibile(hass: HomeAssistant, pod: str) -> date | None:
    """Ultima data (locale) effettivamente presente nelle external
    statistics per il POD, o None se non c'è ancora nessun dato importato."""
    return await async_ultima_data_disponibile(hass, sanitize_statistic_id(pod))
