#!/usr/bin/env python3
"""Verifica da terminale per SET Distribuzione: login B2C, scoperta
della fornitura, una vista giorno della curva - senza Home Assistant.

Stesso ruolo di verify_ireti/areti/edistribuzione_login.py: molto più
veloce della UI di Home Assistant e l'unico modo per sapere se un
cambiamento lato SET/B2C ha rotto qualcosa. NON scrive nulla su disco e
NON stampa identificativi personali (codice fiscale, POD, matricola,
token): solo forme, conteggi e totali.

Uso (credenziali SOLO da variabili d'ambiente, mai come argomenti):

    SET_EMAIL=... SET_PASSWORD=... python3 scripts/verify_set_login.py [YYYY-MM-DD]

Il giorno opzionale è quello di cui scaricare la curva (default: ieri).
Con --refresh prova anche il flusso refresh_token (inferito, mai
osservato in una cattura). Con --wrong-password prova un login con
password sbagliata per confermare la forma dell'errore di B2C.

Va lanciato dalla radice del repository (importa il pacchetto direttamente
dai sorgenti, senza dipendere da homeassistant: auth.py/api.py/const.py
non lo importano).
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import aiohttp

SET_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "contatore_letture"
    / "distributors"
    / "set"
)


def _carica_pacchetto():
    """Carica const/auth/api saltando gli __init__.py (che importano HA)."""
    pkg_name = "set_verify"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(SET_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(modname: str):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{modname}", SET_DIR / f"{modname}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{modname}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("const")
    return _load("auth"), _load("api")


def _maschera(valore: str | None, visibili: int = 0) -> str:
    if not valore:
        return "<vuoto>"
    return f"<{len(valore)} caratteri>" if not visibili else f"{valore[:visibili]}…"


async def main() -> int:
    email = os.environ.get("SET_EMAIL")
    password = os.environ.get("SET_PASSWORD")
    if not email or not password:
        print("Imposta SET_EMAIL e SET_PASSWORD nell'ambiente (non come argomenti).")
        return 2

    argomenti = [a for a in sys.argv[1:] if not a.startswith("--")]
    giorno = date.fromisoformat(argomenti[0]) if argomenti else date.today() - timedelta(days=1)
    prova_refresh = "--refresh" in sys.argv
    prova_password_sbagliata = "--wrong-password" in sys.argv

    auth_mod, api_mod = _carica_pacchetto()

    async with aiohttp.ClientSession(cookie_jar=auth_mod.crea_cookie_jar()) as session:
        auth = auth_mod.SetAuthClient(session)

        if prova_password_sbagliata:
            print("[0] login con password sbagliata (atteso: SetInvalidCredentials)...")
            try:
                await auth.async_login(email, password + "-sbagliata")
                print("    INATTESO: il login è riuscito?!")
            except auth_mod.SetInvalidCredentials as err:
                print(f"    OK, rifiutato: {err}")
            except auth_mod.SetAuthError as err:
                print(f"    ATTENZIONE: errore generico invece di credenziali rifiutate: {err}")

        print("[1] login B2C (authorize -> SelfAsserted -> confirmed -> token)...")
        try:
            tokens = await auth.async_login(email, password)
        except auth_mod.SetInvalidCredentials as err:
            print(f"    credenziali rifiutate: {err}")
            return 1
        except auth_mod.SetAuthError as err:
            print(f"    login fallito: {err}")
            return 1
        print(
            f"    OK: access_token {_maschera(tokens.access_token)}, scade tra {tokens.expires_in}s; "
            f"refresh_token {_maschera(tokens.refresh_token)}, scade tra {tokens.refresh_token_expires_in}s"
        )

        if prova_refresh and tokens.refresh_token:
            print("[1b] refresh_token (flusso inferito)...")
            try:
                nuovi = await auth.async_refresh(tokens.refresh_token)
                print(f"    OK: nuovo access_token {_maschera(nuovi.access_token)}, expires_in {nuovi.expires_in}")
                tokens = nuovi
            except auth_mod.SetAuthError as err:
                print(f"    refresh fallito: {err}")

        api = api_mod.SetApiClient(session, tokens.access_token)

        print("[2] /secure/profile/registration...")
        registrazione = await api.async_get_registration()
        profili = list(registrazione.get("profiles") or [])
        profilo = api_mod.scegli_profilo(profili)
        print(
            f"    fiscalCode {_maschera(registrazione.get('fiscalCode'))}, "
            f"customerType {registrazione.get('customerType')!r}, profiles {profili!r} -> uso {profilo!r}"
        )

        print("[3] /secure/utility/{CF}/active...")
        utilities = await api.async_get_active_utilities(registrazione["fiscalCode"], profilo)
        if not utilities:
            print("    nessuna fornitura elettrica attiva (account Prospect?)")
            return 1
        for u in utilities:
            print(
                f"    fornitura: POD {_maschera(u.get('podPdr'), 6)}, contractId {_maschera(u.get('contractId'))}, "
                f"utilityType {u.get('utilityType')!r}, utilityStatus {u.get('utilityStatus')!r}"
            )
        u = utilities[0]

        print(f"[4] vista mese {giorno.year}-{giorno.month:02d}...")
        mese = await api.async_get_consumption_month(
            registrazione["fiscalCode"], u["contractId"], u["podPdr"], profilo, giorno.year, giorno.month
        )
        giorni_pubblicati = sorted(int(e["day"]) for e in mese if "day" in e)
        print(f"    {len(mese)} giorni pubblicati: {giorni_pubblicati[:3]}…{giorni_pubblicati[-3:]}" if mese else "    vuota")

        print(f"[5] vista giorno {giorno}...")
        vista = await api.async_get_consumption_day(
            registrazione["fiscalCode"], u["contractId"], u["podPdr"], profilo, giorno
        )
        if not vista:
            print("    vuota (giorno non ancora pubblicato?)")
            return 0
        ore = sorted(int(e.get("hour", -1)) for e in vista)
        n_quarti = {len(e.get("quarters") or []) for e in vista}
        totale = sum(sum(q for q in (e.get("quarters") or []) if q is not None) for e in vista)
        totale_dichiarato = sum(float(e.get("total") or 0) for e in vista)
        k = {str(e.get("kConstant")) for e in vista}
        stimati = sum(1 for e in vista if e.get("estimated"))
        print(
            f"    {len(vista)} elementi, ore {ore[0]}..{ore[-1]}, quarti per ora {sorted(n_quarti)}, "
            f"kConstant {sorted(k)}, stimati {stimati}"
        )
        print(f"    somma dei quarti {totale:.3f} kWh, somma dei total {totale_dichiarato:.3f} kWh")
        chiavi = sorted({k for e in vista for k in e})
        print(f"    chiavi: {chiavi}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
