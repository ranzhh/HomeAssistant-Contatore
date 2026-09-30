"""Costanti del protocollo SET Distribuzione (myset.setdistribuzione.it,
gruppo Dolomiti Energia).

Verificato su dati reali il 30/09/2026 (cattura HAR Chrome di un account
con profilo Retail_SET e un POD attivo): login Azure AD B2C, scoperta
del POD dall'account ed endpoint di consumo con curva a 15 minuti.
Dettagli completi e "perché" in
documentation/protocols/set-distribuzione-protocol.md.

DOMAIN NON sta qui: è unificato a livello di contatore_letture (vedi il
const.py principale), stesso principio di pcf_common/edistribuzione/
areti/ireti.
"""
from __future__ import annotations

# --- Portale (SPA React + Azure AD B2C) ----------------------------------
# Tutti i valori qui sotto vengono da window.privatearea (config esposta
# dalla SPA al caricamento) e dalla query string della richiesta
# /authorize osservata nella cattura - vedi set-distribuzione-protocol.md.
BASE_API = "https://agw.setdistribuzione.it/api"
PORTAL_URL = "https://myset.setdistribuzione.it"

AUTH_DOMAIN = "sportelloclientib2cset.b2clogin.com"
TENANT = "sportelloclientib2cset.onmicrosoft.com"
POLICY = "b2c_1a_signin"
CLIENT_ID = "3cfe517e-7c9e-433e-a9dd-daf4cca499c9"  # client pubblico MSAL: nessun secret
REDIRECT_URI = PORTAL_URL
# Lo scope dell'API più quelli OIDC standard: è esattamente la stringa che
# MSAL.js manda nella richiesta /authorize (offline_access = refresh_token).
SCOPE_API = f"https://{TENANT}/private/api/read"
SCOPE = f"{SCOPE_API} openid profile offline_access"

AUTHORIZE_URL = f"https://{AUTH_DOMAIN}/{TENANT}/{POLICY}/oauth2/v2.0/authorize"
TOKEN_URL = f"https://{AUTH_DOMAIN}/{TENANT}/{POLICY}/oauth2/v2.0/token"

DISPLAY_NAME = "SET Distribuzione"
# Confermata via query ARERA live (comuni di Trento, Rovereto e Riva del
# Garda, 30/09/2026): "SET DISTRIBUZIONE S.P.A.", stessa P.IVA del footer
# del sito pubblico. E' la chiave che fa scattare il routing automatico in
# distributors/__init__.py (PIVA_TO_KEY) - vedi il caso Ireti (const.py di
# ireti) per cosa succede se non coincide con quella registrata da ARERA.
PIVA = "01932800228"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
# Lista di codici POD (stringhe): scoperti dall'account durante il config
# flow (/secure/utility/{fiscalCode}/active), non inseriti a mano - come
# edistribuzione/ireti, non come areti/pcf.
CONF_PODS = "pods"
# Identificativi stabili dell'account, risolti una volta nel config flow e
# salvati sulla entry perché servono in OGNI chiamata di consumo (fanno
# parte del path/query): il codice fiscale (o P.IVA per un'utenza
# business - non verificato) e il profilo mySET ("Retail_SET" osservato).
CONF_FISCAL_CODE = "fiscal_code"
CONF_PROFILE = "profile"
# {pod: contractId} - il contractId è l'identificativo (10 cifre, stringa)
# del contratto di distribuzione che compare nel path dell'endpoint di
# consumo. Se manca per un POD (entry creata da una versione precedente o
# POD aggiunto dalle opzioni) il coordinator lo ririsolve da /active.
CONF_CONTRACT_IDS = "contract_ids"

# Headers "da browser": non è verificato che il gateway li richieda (a
# differenza del WAF di Ireti), ma sono quelli che la SPA manda davvero,
# quindi li si replica per non discostarsi dal traffico osservato.
HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9",
    "Referer": f"{PORTAL_URL}/",
    "Origin": PORTAL_URL,
}

# --- Parametri fissi dell'endpoint di consumo ------------------------------
# Enum del bundle JS (condiviso con myDOLOMITI): consumptionType A1 = energia
# attiva prelevata, A2 = immessa (produzione), Q1/Q3/Q4 = reattiva.
# consumptionRange: "F1_F2_F3" (fasce) o "PEAK_OFF_PICK"; la SPA usa sempre
# F1_F2_F3 per la curva e il valore non cambia la forma della risposta
# osservata (quarters/total per ora), ma non è stato provato l'altro.
CONSUMPTION_TYPE_ATTIVA = "A1"
CONSUMPTION_RANGE = "F1_F2_F3"

# utilityType dell'elenco /active: si configurano solo le forniture
# elettriche (il portale gestisce anche gas/teleriscaldamento per altri
# profili del gruppo, mai visti su un account SET).
UTILITY_TYPE_ENERGIA = "ENERGY"
UTILITY_STATUS_ATTIVA = "ACTIVE"

# --- Token ---------------------------------------------------------------------
# access_token 1h (expires_in 3600), refresh_token 24h
# (refresh_token_expires_in 86400) - entrambi osservati nella risposta del
# token endpoint. Il coordinator tiene i token in memoria e usa il refresh
# finché vale, rifacendo il login B2C completo (5 richieste) solo quando
# serve: a differenza di Ireti (refresh da 30 min) qui conviene.
# Margine di sicurezza prima della scadenza nominale.
MARGINE_SCADENZA_TOKEN_SECONDI = 120

# --- Import automatico curva giornaliera + coda di retry ----------------------
# Stesso meccanismo di edistribuzione/ireti (coda per POD dei giorni da
# riprovare, abbandono a tempo). Differenza importante rispetto a Ireti:
# l'API NON accetta un intervallo di date arbitrario - la granularità a
# 15 minuti si ottiene SOLO con la vista "giorno" (year+month+day), una
# chiamata per giorno. Recuperare un arretrato di N giorni costa N
# chiamate (leggere: ~5 KB l'una), quindi le code sono tenute corte.

# Il ciclo è orario (non giornaliero come Ireti): serve per rispettare
# l'orario di cortesia qui sotto senza dipendere dall'ora di avvio di Home
# Assistant. Un ciclo in cui non c'è nulla da chiedere NON fa nemmeno il
# login (vedi coordinator.py), quindi il costo dei cicli "a vuoto" è zero
# lato portale.
DEFAULT_UPDATE_INTERVAL_MINUTES = 60

# Unica osservazione reale sul ritardo di pubblicazione: alle 17:56 locali
# del 30/09/2026 la vista mensile conteneva tutti i giorni fino al 29
# compreso (e nessun dato del 30). Quindi "il giorno prima" è disponibile
# almeno dal tardo pomeriggio - non sappiamo se lo sia già la mattina.
RITARDO_DATI_GIORNI = 1

# Ora locale (0-23) a partire dalla quale chiedere il giorno precedente,
# configurabile dalle opzioni (stessa chiave "ora_richiesta" di
# edistribuzione, così lo step "orario" dell'options flow è condiviso).
# 18 è l'ora della sola osservazione disponibile (vedi sopra): un valore
# prudente, da abbassare se emerge che i dati arrivano prima.
CONF_ORA_RICHIESTA = "ora_richiesta"
ORA_MINIMA_RICHIESTA = 18

CONF_GIORNI_DA_RIPROVARE = "giorni_da_riprovare"

# Un giorno resta in coda e viene riprovato ai cicli successivi, abbandonato
# dopo questo numero di giorni REALI dal primo inserimento (non dopo N
# tentativi). Stesso valore di edistribuzione/ireti.
ABBANDONO_CODA_DOPO_GIORNI = 7

# Con una chiamata per giorno, la coda è anche il tetto al numero di
# chiamate di un singolo ciclo: 30 chiamate leggere restano accettabili.
MAX_GIORNI_IN_CODA = 30

# Limite di cortesia per l'azione recupera_storico (auto-imposto): il
# recupero passa dall'export Excel mensile (una chiamata per mese, vedi
# excel.py), quindi due anni sono ~24 chiamate - stesso limite di Ireti.
MAX_GIORNI_RECUPERO_STORICO = 731
