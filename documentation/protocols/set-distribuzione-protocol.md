# SET Distribuzione — protocollo e design

**SET Distribuzione S.p.A.** (Rovereto, gruppo Dolomiti Energia) — portale
`myset.setdistribuzione.it`. **Supportato**: login, POD scoperti
dall'account e curva di carico a **15 minuti** importata come external
statistics, con una coda dei giorni da riprovare per POD (stesso
meccanismo di E-Distribuzione/Ireti — vedi
[Design del coordinator](#design-del-coordinator)).

L'endpoint di consumo è **confermato con dati reali** (30/09/2026):
cattura HAR Chrome di un account con profilo `Retail_SET` e un POD
attivo, che ha risolto le tre domande aperte della versione precedente
di questa scheda (account non-Prospect, dati di misura vs fatturazione,
header `Authorization`).

**Non ancora testato dentro Home Assistant** su un'installazione reale:
config flow, import automatico e sensori sono coperti dai test unitari;
login (anche con password sbagliata), refresh, anagrafica, forniture,
viste anno/mese/giorno ed export Excel sono confermati da terminale
(`scripts/verify_set_login.py`, 30/09/2026) sullo stesso account della
cattura, non ancora da un uso prolungato. Vedi
[Cosa resta aperto](#cosa-resta-aperto).

__Le informazioni qui sotto vengono dall'analisi del traffico del portale
(cattura HAR) e dal bundle JavaScript dell'app, non da documentazione
ufficiale (che non esiste pubblicamente). Ogni sezione dice se è
"verificato" (visto nella cattura) o "inferito" (dedotto dal bundle o dal
comportamento standard di Azure AD B2C, mai osservato direttamente).__

## Quadro generale

Il login passa da **Azure AD B2C** (Microsoft), non dal protocollo PCF
(Duereti/Unareti) né da quello Salesforce/OTP di E-Distribuzione né dal
Keycloak di Ireti: protocollo strutturalmente nuovo, **caso B** di
[`CONTRIBUTING.md`](../../CONTRIBUTING.md), pacchetto `distributors/set/`.

La SPA (`myset.setdistribuzione.it`) è una web app React che espone la
sua config al caricamento come `window.privatearea` — verificato:

```
BASE_API     = https://agw.setdistribuzione.it/api
AUTH_DOMAIN  = sportelloclientib2cset.b2clogin.com
TENANT       = sportelloclientib2cset.onmicrosoft.com
POLICY       = b2c_1a_signin
CLIENT_ID    = 3cfe517e-7c9e-433e-a9dd-daf4cca499c9   (client pubblico MSAL, nessun secret)
REDIRECT_URI = https://myset.setdistribuzione.it
SCOPE        = https://sportelloclientib2cset.onmicrosoft.com/private/api/read openid profile offline_access
PIVA         = 01932800228
```

> [!NOTE]
> **P.IVA confermata via query ARERA live** (30/09/2026, comuni di
> Trento, Rovereto e Riva del Garda): `SET DISTRIBUZIONE S.P.A.`,
> `01932800228` — coincide con quella del footer del sito. Il
> riconoscimento automatico del distributore nel wizard funziona quindi
> per i comuni serviti da SET (a Riva del Garda ARERA restituisce anche
> `ALTO GARDA SERVIZI SPA`: il wizard fa scegliere).

> [!NOTE]
> Il bundle JS della SPA è **condiviso con myDOLOMITI** (portale del
> venditore del gruppo): stesso codice React/Redux Toolkit Query, stesso
> `authDomain` di default (`dolomitienergiab2c.b2clogin.com`)
> sovrascritto a runtime da `window.privatearea`. Per questo alcuni
> endpoint/enum del bundle (gas, teleriscaldamento, bollette) non hanno
> senso per un account SET puro.

## Login — verificato con cattura reale (30/09/2026)

Flusso standard Azure AD B2C "sign in" via MSAL.js (Authorization Code +
PKCE), pagina di login ospitata su `b2clogin.com` con UI custom
(`signuporsignin-set-ui.html` su uno storage Azure). **Nessun OTP, nessun
captcha, nessuna MFA** osservati: cinque richieste in sequenza,
riproducibili senza browser (`auth.py`). Le richieste 2-3 dipendono dai
cookie `x-ms-cpim-*` impostati dalla 1 (serve un cookie jar).

1. `GET {AUTH_DOMAIN}/{TENANT}/{POLICY}/oauth2/v2.0/authorize`
   con `client_id`, `scope`, `redirect_uri`, `response_type=code`,
   `response_mode=fragment`, `code_challenge` + `code_challenge_method=S256`,
   `state`, `nonce`, `client_info=1`, `client-request-id` (uuid),
   `x-client-SKU=msal.js.browser`, `x-client-VER=2.39.0`.
   Risposta: HTML con un blocco `var SETTINGS = {...};` (JSON) — verificato:

   ```json
   {
     "remoteResource": "https://.../signuporsignin-set-ui.html",
     "api": "CombinedSigninAndSignup",
     "csrf": "<token anti-CSRF>",
     "transId": "StateProperties=<id transazione>",
     "hosts": {
       "tenant": "/sportelloclientib2cset.onmicrosoft.com/B2C_1A_signin",
       "policy": "B2C_1A_signin",
       "static": "https://sportelloclientib2cset.b2clogin.com/static/"
     },
     "config": { "operatingMode": "Email", "enableRememberMe": "true", "showSignupLink": "false" },
     "locale": { "lang": "it" }
   }
   ```

   Nota la **maiuscola** in `hosts.tenant`/`hosts.policy` (`B2C_1A_signin`):
   i passi 2 e 3 vanno fatti su quel path, non su quello minuscolo di
   `/authorize`.
2. `POST {AUTH_DOMAIN}{hosts.tenant}/SelfAsserted?tx={transId}&p={hosts.policy}`
   — form `application/x-www-form-urlencoded`, header `X-CSRF-TOKEN: {csrf}`
   e `X-Requested-With: XMLHttpRequest`. Risposta verificata: `{"status": "200"}`.
   Il body del POST è redatto nella cattura (contiene la password): i
   campi sono **inferiti** dalla UI custom (`<input id="signInName">`,
   `<input id="password">`) e dallo script B2C che li serializza
   (`request_type=RESPONSE&<id>=<valore>&...`), quindi
   `request_type=RESPONSE&signInName=<email>&password=<password>`. Con
   credenziali sbagliate B2C risponde comunque **HTTP 200** con
   `{"status": "400", "errorCode": "AADB2C90053", "message": "Credenziali non valide."}`
   — **verificato** dal vivo il 30/09/2026 con un account inesistente:
   `auth.py` tratta qualunque `status != "200"` come credenziali rifiutate.
   Se dopo un rifiuto si chiama comunque `confirmed`, B2C fa 302 verso
   `{REDIRECT_URI}/#error=server_error&error_description=AADB2C90255...`
   (verificato).
3. `GET {AUTH_DOMAIN}{hosts.tenant}/api/CombinedSigninAndSignup/confirmed?rememberMe=true&csrf_token={csrf}&tx={transId}&p={hosts.policy}`
   → **302** con `Location: https://myset.setdistribuzione.it/#state=...&client_info=...&code=...`
   (verificato: il `code` è nel **fragment**, coerente con
   `response_mode=fragment`). `auth.py` non segue il redirect, legge
   l'header.
4. `POST {AUTH_DOMAIN}/{TENANT}/{POLICY}/oauth2/v2.0/token` (form) con
   `grant_type=authorization_code`, `code`, `code_verifier`, `client_id`,
   `redirect_uri`, `scope`, `client_info=1`. Risposta verificata (valori omessi):

   ```json
   {
     "access_token": "<JWT>", "id_token": "<JWT>", "token_type": "Bearer",
     "not_before": 1790783740, "expires_in": 3600, "expires_on": 1790787340,
     "resource": "<guid dell'API>", "client_info": "<base64>",
     "scope": "https://sportelloclientib2cset.onmicrosoft.com/private/api/read",
     "refresh_token": "<opaco>", "refresh_token_expires_in": 86400
   }
   ```

`access_token` dura **1h**, `refresh_token` **24h**. Il refresh
(`grant_type=refresh_token` sullo stesso endpoint, con `client_id`,
`refresh_token`, `scope`, `client_info=1`) — **verificato** da terminale
il 30/09/2026 (`scripts/verify_set_login.py --refresh`): nuovo
`access_token` con `expires_in 3600`. Il coordinator lo usa e ricade sul
login completo se fallisce.

> [!WARNING]
> **Cookie jar: `aiohttp.CookieJar(quote_cookie=False)` è obbligatorio.**
> Il jar di default di aiohttp rimanda tra virgolette i valori dei cookie
> con caratteri speciali, e i cookie di transazione B2C (`x-ms-cpim-csrf`,
> `x-ms-cpim-trans`, `x-ms-cpim-cache|<id>_0`) ne sono pieni: B2C risponde
> `400 Bad Request` (HTML, corpo `Bad Request`) a **qualsiasi** richiesta
> che li porti, incluso un secondo `/authorize` nella stessa sessione.
> Verificato dal vivo il 30/09/2026 senza credenziali (basta `/authorize`
> due volte). Con `quote_cookie=False` tutto passa. `auth.crea_cookie_jar()`
> restituisce il jar giusto e `SetAuthClient` rifiuta subito una sessione
> con un jar che quota.

> [!NOTE]
> **Header `Authorization: Bearer <access_token>`** su tutte le chiamate
> a `agw.setdistribuzione.it`: **verificato** (cattura Chrome, che a
> differenza di Safari non redige gli header). Nessun header custom,
> nessuna subscription key, nessun cookie di sessione in aggiunta. Il
> dubbio della scheda precedente è chiuso.

## API — anagrafica e scoperta del POD (verificate)

Tutte su `{BASE_API}` con Bearer token; ogni risposta ha la forma
`{"message": "<testo>", "data": ...}`. Il codice fiscale (`{CF}`) e il
profilo (`profile=`) compaiono in quasi tutte le chiamate: si prendono
da `/secure/profile/registration`.

| Endpoint | Cosa restituisce (`data`) |
|---|---|
| `GET /secure/profile/registration` | `email`, `cellphone`, `givenName`, `surname`, `fiscalCode`, `customerType` (`"RETAIL"`), **`profiles`** (`["Retail_SET"]`), `assistant`/`superAdmin`/`isImpersonating`/`isReadOnly` (bool), `displayName` |
| `GET /secure/profile/customer` | lista di profili: `fiscalCode`, `businessPartner`, `profile` (`"Retail_SET"`), `defaultProfile`, `masterBP`, `myDolomiti` (`"REGISTERED"`), `supportContacts`... |
| `GET /secure/utility/{CF}/active?profile=Retail_SET` | **`utilities[]`**: `businessPartner`, `name`, `utilityStatus` (`"ACTIVE"`), **`podPdr`** (il POD), `utilityType` (`"ENERGY"`), `utilityAddress`, **`contractId`** (10 cifre, stringa), `pesseGroup`; più `count`, `profile`, `utilityTypes` |
| `GET /secure/utility/detail/{CF}/{contractId}?profile=` | `status`, `address`, `meterSerialNumber` (15 caratteri), `meterType` (`"2G"`), `pod`, `contractualPower`, `availablePower`, `voltage` (`"400"`), `voltageType` (`"BT"`), `phase` (`"MONOFASE"`), `tariffCategory`, `helpContact`... |
| `GET /secure/utility/reports/{CF}?profile=` | `count`, `page`, `pageCount`, `supplyType` — vuoto nella cattura |
| `POST /secure/utility/pesse/emergency` | piano di emergenza PESSE (nome, indirizzo, POD, `detachmentGroup`) — non usato |
| `GET /b2c/alert` (senza token) | banner applicativo `show`/`message`/`iconName` |

**Come si scoprono gli identificativi** (`config_flow.py`, `coordinator.py`):

1. `registration` → `fiscalCode` e `profiles`. Il profilo da usare è
   l'unico presente (`Retail_SET`); con più profili (account del gruppo
   con anche gas/myDOLOMITI) `api.scegli_profilo` preferisce quello con
   suffisso `_SET`. `Business_SET` esiste nell'enum del bundle — mai
   visto; un'utenza business avrà presumibilmente la P.IVA al posto del
   CF nel path (**inferito**).
2. `active` → per ogni fornitura `podPdr` + `contractId`. Si tengono solo
   `utilityType == "ENERGY"` e `utilityStatus == "ACTIVE"`.
3. Il config flow salva sulla entry `fiscal_code`, `profile` e
   `contract_ids` (`{pod: contractId}`): servono a ogni chiamata di
   consumo, così il ciclo automatico non deve rifare l'anagrafica.

`profiles: ["Prospect"]` (la cattura precedente) è un account senza
fornitura: `active` torna vuoto e il wizard lo dice chiaramente
(`no_set_pods_associated`).

## Endpoint di consumo — verificato nelle tre viste (30/09/2026)

```
GET /secure/utility/{CF}/{contractId}/consumption
    ?consumptionRange=F1_F2_F3&consumptionType=A1&profile=Retail_SET
    &supplyPoint={POD}&year=2026[&month=8[&day=20]]
```

> [!WARNING]
> **`month` è 0-based** (gennaio = 0): il frontend fa `value -= 1` prima
> della chiamata (visibile nel bundle) e la cattura lo conferma —
> `month=8` ha restituito i giorni di **settembre**. Il campo `month`
> **dentro** la risposta è invece 1-based (`"month": 9`). `api.py`
> accetta mesi 1-based e converte da solo. (Nella vista annuale c'è
> anche un bug di etichetta lato server: `month: 2` con
> `descMonth: "March"` — innocuo, non si usa `descMonth`.)

Il path ha **un solo** segmento id (`{contractId}`), non due come nel
builder `getUtilityConsumptionUsingGet` del bundle
(`{fiscalCode}/{contractualAccount}/{contractId}`): per un account SET
la SPA usa la variante a un id. Parametri fissi: `consumptionType=A1`
(energia attiva prelevata; enum: `A1`, `A2` immessa, `Q1`/`Q3`/`Q4`
reattiva) e `consumptionRange=F1_F2_F3` (l'alternativa `PEAK_OFF_PICK`
non è stata provata; non cambia la forma della risposta osservata).

La **granularità dipende da quali di `year`/`month`/`day` sono presenti**
— non esiste un intervallo di date arbitrario:

| Parametri | `data.consumptions[]` | Altro in `data` |
|---|---|---|
| `year` | un elemento per **mese**: `year`, `month` (1-based), `descMonth`, `total` (kWh), `average`, `estimated`, `drillDownAvailable`, `meterSerialNumber` | `total`, `average` |
| `year`+`month` | un elemento per **giorno** pubblicato: `year`, `month`, `day`, `hour: 0`, `total` (kWh del giorno), `average`, `estimated`, `kConstant`, `meterSerialNumber` | `total`, `average`, `powerPeak` (stringa, es. `"domenica 20 ore 13: 3,620 kW"`) |
| `year`+`month`+`day` | **24 elementi, uno per ora**: come sopra più **`quarters`** (4 valori) | idem |

Elemento della vista giorno (valori reali, identificativi omessi):

```json
{
  "drillDownAvailable": true,
  "year": 2026, "month": 9, "day": 20, "hour": 13,
  "quarters": [0.117, 0.124, 0.289, 0.046],
  "total": 0.576,
  "average": 0.144,
  "estimated": false,
  "meterSerialNumber": "<matricola>",
  "kConstant": "1"
}
```

- **Unità: kWh per quarto d'ora** — verificato in tre modi: l'export
  Excel dello stesso portale dichiara `Unità di misura: kWh`; `total` di
  ogni ora è esattamente la somma dei 4 `quarters` (controllato su tutte
  le 24 ore); i totali mensili del portale coincidono con quelli letti
  sul contatore fisico della fornitura della cattura. **Sono letture del distributore,
  non dati di fatturazione** (il dubbio della scheda precedente è
  chiuso).
- `quarters[0]` = hh:00-hh:15, `[3]` = hh:45-hh:00, ora **locale**
  (Europe/Rome), nessun offset nella risposta.
- `kConstant`: `"1"` ovunque. Non è noto se con K ≠ 1 i valori arrivino
  già moltiplicati — `statistics.py` non moltiplica e avvisa nei log.
- `estimated`: `false` ovunque nella cattura. I valori stimati vengono
  importati comunque (import idempotente: il dato reale, quando arriva,
  sostituisce quello stimato per la stessa ora).
- **Un giorno non ancora pubblicato NON torna vuoto** (verificato il
  30/09/2026 chiedendo il giorno stesso): la vista giorno risponde 200
  con **24 segnaposto** `{"year","month","day","hour","total": 0.0,
  "estimated": true}` **senza `quarters`**, e `data.total = 0.0`. La
  vista mensile invece non contiene proprio il giorno. Quindi "c'è il
  dato" ⇔ "almeno un elemento ha `quarters`" (`api.giorno_pubblicato`):
  importare i segnaposto scriverebbe 24 ore a zero e il giorno
  sparirebbe dalla coda. `statistics.py` scarta ogni elemento
  `estimated` senza `quarters`.
- **Anno/mese senza nessun dato → HTTP 404** `{"statusCode": 404,
  "message": "Resource not found"}` (verificato sulla vista anno 2023).
  `api.py` lo tratta come lista vuota sulle viste di consumo, non come
  errore.
- **Profondità dello storico** (contatore della cattura, 2G): vista anno
  2025 con 12 mesi tutti `drillDownAvailable: true`; 2024 con 11 mesi di
  cui solo 3 drillabili (i dati a 15 minuti partono da ottobre 2024,
  presumibilmente l'installazione del 2G); 2023 → 404. La vista mese di
  settembre 2025 aveva 30 giorni. `recupera_storico` legge la vista anno
  e salta i mesi non drillabili invece di chiedere 30 giorni a vuoto.
- `consumptionRange=PEAK_OFF_PICK` sulla vista giorno: stessa identica
  forma di `F1_F2_F3` (verificato), il parametro non influisce sulla
  curva.

**Ritardo di pubblicazione** — una sola osservazione: alle **17:56
locali del 30/09/2026** la vista mensile aveva tutti i giorni fino al
**29** compreso. Quindi il giorno prima è disponibile almeno dal tardo
pomeriggio; non sappiamo se lo sia già la mattina (vedi
`ORA_MINIMA_RICHIESTA` in `const.py`, 18, configurabile dalle opzioni).

**Cambio ora legale**: NON verificato (la cattura è di settembre). Non
sappiamo se il giorno da 23/25 ore abbia 23/25 elementi, un `hour`
ripetuto o un'ora vuota. `statistics.py` scarta con un avviso gli
`hour` fuori da 0-23; il caso "25 ore" va guardato a fine ottobre 2026.

### Export Excel (verificato)

```
GET /secure/utility/{CF}/{contractId}/consumption/excel
    ?consumptionType=A1&master=true&month=8&profile=Retail_SET&supplyPoint={POD}&year=2026
```

Risposta JSON, non un file: `{"message": "Utility's consumptions  excel
file retrieved", "data": {"fileName": "Consumi_<POD>_settembre_2026.xlsx",
"file": "<xlsx in base64>"}}` (~140 KB per un mese). Il foglio
`Energia Attiva Prevelata` ha righe di intestazione (POD, Ruolo, Unità di
misura = kWh, Anno, Mese) poi `Giorno | Da ora | A ora | Valore |
Costante K | Matricola Contatore`, 96 righe per giorno, `Giorno` in
`dd/mm/yyyy`, ore `HH:MM`, `Valore` con la virgola decimale (es. `0,020`),
K = 1; un secondo foglio `Energia Reattiva Q1` ha lo stesso layout.
Verificato il 30/09/2026 anche su agosto 2026: 2976 righe (31 × 96) nel
foglio attiva, e la **somma dei `Valore` coincide al millesimo con la
somma dei `total` della vista mese JSON** (267,259 kWh). È l'unica strada
a **una chiamata per mese** per la curva a 15 minuti, ed è quella che
usa `recupera_storico` (`excel.py`, parser con `openpyxl`, aggiunto ai
`requirements` del manifest per questo; parser verificato sul file reale di
agosto 2026: 31 giorni, 744 ore, 267,259 kWh): ≈1 chiamata per mese invece di
~30, con ripiego automatico alla vista giorno JSON se il file di un mese
manca o non è parsabile. Il ciclo automatico resta sulla vista giorno
(un giorno alla volta è esattamente quello che serve lì).

### Altri endpoint provati dal vivo (30/09/2026)

| Endpoint | Esito |
|---|---|
| `GET /secure/utility/{CF}/consumption/excel/{year}/{month}?profile=` ("tutte le forniture", dal bundle) | **404** con `month` sia 0-based sia 1-based: non disponibile per un profilo Retail_SET |
| `GET /secure/utility/{CF}/{contractId}/{meterSerialNumber}/meter-read?profile=&year=` | **404** per 2025 e 2026 (`pdc` = contractId è un'ipotesi: potrebbe essere un altro identificativo) |
| `GET .../{contractId}/totalizers/excel?consumptionType=A1&master=true&year=&month=` (0-based) | **200**: xlsx `Totalizzatori_<POD>_<mese>_<anno>.xlsx` con una riga per giorno e le **letture cumulative di registro** per fascia (`Fascia F1..F6`, `Totale`) — il delta giornaliero del `Totale` è il consumo del giorno: un cross-check gratuito della curva, non usato |

### Altri endpoint del bundle — MAI chiamati, non verificati

Estratti dalle definizioni RTK Query del bundle (analisi statica): utili
solo come mappa per chi volesse esplorare oltre.

| Nome (RTK Query) | Metodo | Path (relativo a `{BASE_API}`) | Query params |
|---|---|---|---|
| `getSuppliesConsumptionFileUsingGet` | GET | `/secure/utility/{fiscalCode}/consumption/excel/{year}/{month}` | `profile` |
| `getMultisiteUtilityDetailsUsingGet` | GET | `/secure/utility/{fiscalCode}/{contractualAccount}` | `profile` |
| `getUtilityMeterSelfReadingUsingGet` | GET | `.../{contractId}/self-reading/{utilityType}/{utilityPoint}` | `profile` |
| `postUtilityMeterSelfReadingUsingPost` | POST | idem | — (autolettura) |
| `getUtilityMeterReadFileUsingGet` | GET | `.../{contractId}/{meterSerialNumber}/meter-read/excel` | `profile`, `year` |
| `getUtilityMeterReadUsingGet` | GET | `/secure/utility/{fiscalCode}/{pdc}/{meterSerialNumber}/meter-read...` | — |
| `getBillsUsingGet` | GET | `/secure/bill/{fiscalCode}` | `billStatus`, `contractualAccount`, `month`, `page`, `podPdr`, `profile`, ... |
| `getUtilityDocumentsUsingGet` | GET | `/secure/utility/documents/{fiscalCode}` | `commodity`, `month`, `page`, `podPdr`, `profile`, ... |

## Design del coordinator

- **Una chiamata per giorno.** La curva a 15 minuti esiste solo nella
  vista giorno, quindi non c'è una "finestra scorrevole" in una
  richiesta come Ireti: la coda dei giorni da riprovare (per POD,
  abbandono a tempo come E-Distribuzione) è anche il tetto alle chiamate
  di un ciclo (`MAX_GIORNI_IN_CODA = 30`). Ogni chiamata è leggera
  (~5 KB) e il ciclo tiene, per ogni giorno: risposta piena → import +
  rimozione dalla coda; risposta vuota → in coda; errore → i giorni non
  ancora chiesti finiscono in coda e il ciclo fallisce con `UpdateFailed`.
- **Ciclo orario con orario di cortesia** (`ora_richiesta`, default 18,
  stessa chiave e stesso step delle opzioni di E-Distribuzione): prima
  di quell'ora si chiedono solo gli arretrati, non il giorno prima. Un
  ciclo senza nulla da chiedere **non fa nemmeno il login** — costo zero
  lato portale; il resto del giorno il ciclo si limita a rileggere le
  statistiche locali.
- **Token in memoria**: si riusa l'`access_token` finché vale (1h con
  margine), poi il `refresh_token` (24h), e solo se anche quello manca o
  fallisce si rifà il login B2C completo. A un riavvio di HA si riparte
  da un login. Un 401 inatteso su una chiamata dati forza un re-login e
  un solo retry. Credenziali rifiutate → `ConfigEntryAuthFailed` → reauth
  gestito da HA.
- **Identificativi**: `fiscal_code`/`profile`/`contract_ids` dalla entry;
  se un `contractId` manca (POD aggiunto dalle opzioni, entry vecchia)
  si ririsolve da `active` e si persiste.
- **`recupera_storico`**: prima legge la vista anno e **salta i mesi
  senza `drillDownAvailable`** (e gli anni in 404), poi per ogni mese
  rimasto scarica **l'export Excel mensile** (una chiamata, tutta la
  curva del mese) e lo converte nella forma della vista giorno
  (`excel.py`), così l'import passa dallo stesso percorso del ciclo
  automatico. Se l'Excel di un mese non è disponibile o non è parsabile
  ricade sulla vista giorno, una chiamata per giorno. Limite di cortesia
  auto-imposto: 731 giorni per azione (≈24 chiamate).

## Cosa resta aperto

1. **Cambio ora legale** (vedi sopra) — da verificare con la vista giorno
   del 25/10/2026 e del 29/03/2027.
2. **Orario reale di pubblicazione**: una sola osservazione (≤ 17:56).
   Se i dati del giorno prima ci sono già la mattina, si può abbassare
   `ORA_MINIMA_RICHIESTA`.
3. **`kConstant` ≠ 1** e **utenze business** (P.IVA nel path, profilo
   `Business_SET`): mai visti.
4. **Excel mensile nel giorno di cambio ora**: come per la vista giorno,
   non verificato (8 righe con la stessa `Da ora` nell'ora ripetuta?
   `excel.py` le sommerebbe nella stessa ora).

## Come contribuire

Se hai una fornitura SET attiva e vuoi verificare i punti aperti:
`scripts/verify_set_login.py` fa login, elenca le forniture e scarica un
giorno di curva stampando solo forme e totali, con le credenziali da
variabili d'ambiente (`SET_EMAIL`/`SET_PASSWORD`), senza scrivere nulla su
disco.

Per una **cattura HAR** (Chrome/Firefox, Preserve log, "Export HAR with
content"): la HAR grezza contiene token di sessione, cookie B2C, codice
fiscale, POD, matricola del contatore (17 caratteri), nome e indirizzo —
**non allegarla a una issue pubblica**. Prima di condividerla anche in
privato, anonimizzala con uno script che tolga gli header
`Authorization`/`Cookie`/`Set-Cookie`, i campi `access_token`/
`refresh_token`/`id_token`/`code`/`code_verifier`/`client_info`/`state`/
`nonce`/`csrf`/`tx`, i body dei POST verso `b2clogin.com`, i JWT, il CF,
il POD (anche dentro `fileName` dell'export Excel), email, telefono,
matricola e i base64 (l'export Excel viaggia in base64 dentro un JSON) —
e verifica il risultato con dei conteggi automatici, non a occhio.
