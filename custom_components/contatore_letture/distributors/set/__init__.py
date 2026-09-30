"""Plugin distributore SET Distribuzione (myset.setdistribuzione.it, gruppo
Dolomiti Energia).

Protocollo diverso da tutti gli altri già supportati: login Azure AD B2C
(Authorization Code + PKCE guidato a mano sulla pagina ospitata da B2C,
nessun OTP) + API REST con Bearer token. L'endpoint di consumo (vista
"giorno" con 4 quarti d'ora per ora) è CONFERMATO su dati reali il
30/09/2026. Percorso completo: login (auth.py) -> chiamate REST (api.py)
-> curva importata come external statistics (statistics.py), guidato dal
coordinator (coordinator.py, coda per POD come edistribuzione/ireti ma
con UNA chiamata per giorno, perché l'API non accetta intervalli).
Dettagli completi e "perché" in
documentation/protocols/set-distribuzione-protocol.md.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DISPLAY_NAME, PIVA
from .coordinator import SetCoordinator
from .sensor import build_set_entities

__all__ = ["DISPLAY_NAME", "PIVA", "create_coordinator", "build_sensor_entities"]


def create_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> SetCoordinator:
    return SetCoordinator(hass, entry)


def build_sensor_entities(hass, coordinator: SetCoordinator, entry: ConfigEntry) -> list:
    return build_set_entities(hass, coordinator)
