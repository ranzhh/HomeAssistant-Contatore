"""Fixture per i test che istanziano il SetCoordinator.

Qui serve principalmente per esercitare la coda dei giorni da riprovare
(per-POD): __init__ non fa I/O, il client auth creato resta inutilizzato.
"""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.contatore_letture.const import DOMAIN
from custom_components.contatore_letture.distributors.set import create_coordinator
from custom_components.contatore_letture.distributors.set.const import (
    CONF_CONTRACT_IDS,
    CONF_EMAIL,
    CONF_FISCAL_CODE,
    CONF_PASSWORD,
    CONF_PODS,
    CONF_PROFILE,
)

# Valori di fantasia: la forma (IT + 3 cifre + E + 8 cifre; contractId a
# 10 cifre come stringa) e' quella reale, i valori no.
POD_A = "IT221E00000000001"
POD_B = "IT221E00000000002"
CONTRACT_A = "0010000001"
CONTRACT_B = "0010000002"
CF = "AAABBB00A00A000A"
PROFILE = "Retail_SET"


@pytest.fixture
def make_set_coordinator(hass):
    """Factory: SetCoordinator con una MockConfigEntry agganciata a hass."""

    def _make(*, data=None, pods=None, options=None):
        pods = pods if pods is not None else [POD_A]
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                "distributor": "set",
                CONF_EMAIL: "utente@example.com",
                CONF_PASSWORD: "x",
                CONF_PODS: pods,
                CONF_FISCAL_CODE: CF,
                CONF_PROFILE: PROFILE,
                CONF_CONTRACT_IDS: {POD_A: CONTRACT_A, POD_B: CONTRACT_B},
                **(data or {}),
            },
            options=options or {},
        )
        entry.add_to_hass(hass)
        return create_coordinator(hass, entry)

    return _make
