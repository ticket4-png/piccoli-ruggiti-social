#!/usr/bin/env python3
"""
Piccoli Ruggiti - statistiche giornaliere dei servizi Google (solo lettura):
Google Analytics 4, Search Console e Merchant Center.

Usa un account di servizio Google (chiave JSON nel segreto GitHub GOOGLE_SA_KEY),
aggiunto come Visualizzatore in Analytics, utente Limitato in Search Console e
Sola lettura + Rendimento in Merchant Center.

Scrive statistiche/google.json e aggiunge una riga a statistiche/storico_google.csv.
Ogni servizio è indipendente: se uno dà errore, gli altri vanno avanti e l'errore
finisce in "avvisi".
"""
import csv
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

ROMA = ZoneInfo("Europe/Rome")
QUI = os.path.dirname(os.path.abspath(__file__))
CARTELLA = os.path.join(QUI, "statistiche")

GA_PROPERTY = os.environ.get("GA_PROPERTY", "565053708")
SC_SITE = os.environ.get("SC_SITE", "https://piccoli-ruggiti.myspreadshop.it/")
MERCHANT_ID = os.environ.get("MERCHANT_ID", "5907405906")
SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/content",
]


class Google:
    def __init__(self, chiave_json):
        info = json.loads(chiave_json)
        self.cred = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    def _token(self):
        if not self.cred.valid:
            self.cred.refresh(Request())
        return self.cred.token

    def chiama(self, metodo, url, corpo=None):
        r = requests.request(metodo, url, json=corpo, timeout=60,
                             headers={"Authorization": f"Bearer {self._token()}"})
        try:
            dati = r.json()
        except ValueError:
            dati = {}
        if r.status_code >= 300:
            msg = (dati.get("error") or {}).get("message") or r.text[:300]
            raise RuntimeError(f"HTTP {r.status_code}: {msg}")
        return dati


# ---------------------------------------------------------------- Analytics
def ga_report(g, dimensioni, metriche, giorni=7, limite=10, ordina=None):
    corpo = {
        "dateRanges": [{"startDate": f"{giorni}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": d} for d in dimensioni],
        "metrics": [{"name": m} for m in metriche],
        "limit": limite,
    }
    if ordina:
        corpo["orderBys"] = [{"metric": {"metricName": ordina}, "desc": True}]
    r = g.chiama("POST", f"https://analyticsdata.googleapis.com/v1beta/properties/{GA_PROPERTY}:runReport", corpo)
    righe = []
    for riga in r.get("rows", []):
        voce = {d: v["value"] for d, v in zip(dimensioni, riga.get("dimensionValues", []))}
        for m, v in zip(metriche, riga.get("metricValues", [])):
            try:
                voce[m] = float(v["value"]) if "." in v["value"] else int(v["value"])
            except ValueError:
                voce[m] = v["value"]
        righe.append(voce)
    return righe


def analytics(g):
    base = ["activeUsers", "sessions", "screenPageViews"]
    per_giorno = ga_report(g, ["date"], base, giorni=14, limite=20)
    per_giorno.sort(key=lambda x: x["date"])
    return {
        "per_giorno": per_giorno,
        "sorgenti_7g": ga_report(g, ["sessionSourceMedium"], ["sessions", "activeUsers"], ordina="sessions"),
        "citta_7g": ga_report(g, ["city"], ["activeUsers"], ordina="activeUsers"),
        "dispositivi_7g": ga_report(g, ["deviceCategory"], ["activeUsers"], ordina="activeUsers"),
        "pagine_7g": ga_report(g, ["pageTitle"], ["screenPageViews"], ordina="screenPageViews"),
        "eventi_7g": ga_report(g, ["eventName"], ["eventCount"], ordina="eventCount", limite=15),
    }


# ---------------------------------------------------------------- Search Console
def search_console(g):
    sito = requests.utils.quote(SC_SITE, safe="")
    url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{sito}/searchAnalytics/query"
    fine = date.today()
    inizio = fine - timedelta(days=28)
    periodo = {"startDate": inizio.isoformat(), "endDate": fine.isoformat()}
    out = {}
    tot = g.chiama("POST", url, {**periodo, "rowLimit": 1})
    out["totale_28g"] = (tot.get("rows") or [{}])[0] if tot.get("rows") else {"clicks": 0, "impressions": 0}
    out["per_giorno"] = g.chiama("POST", url, {**periodo, "dimensions": ["date"], "rowLimit": 30}).get("rows", [])
    out["query"] = g.chiama("POST", url, {**periodo, "dimensions": ["query"], "rowLimit": 15}).get("rows", [])
    out["pagine"] = g.chiama("POST", url, {**periodo, "dimensions": ["page"], "rowLimit": 15}).get("rows", [])
    try:
        ins = g.chiama("POST", "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
                       {"inspectionUrl": SC_SITE, "siteUrl": SC_SITE, "languageCode": "it-IT"})
        res = (ins.get("inspectionResult") or {}).get("indexStatusResult") or {}
        out["home_indicizzazione"] = {k: res.get(k) for k in
                                      ("verdict", "coverageState", "robotsTxtState", "indexingState",
                                       "lastCrawlTime", "pageFetchState")}
    except RuntimeError as e:
        out["home_indicizzazione"] = {"errore": str(e)[:200]}
    return out


# ---------------------------------------------------------------- Merchant Center
def merchant_query(g, query):
    righe, token = [], None
    for versione in ("v1", "v1beta"):
        url = f"https://merchantapi.googleapis.com/reports/{versione}/accounts/{MERCHANT_ID}/reports:search"
        try:
            while True:
                corpo = {"query": query, "pageSize": 1000}
                if token:
                    corpo["pageToken"] = token
                r = g.chiama("POST", url, corpo)
                righe += r.get("results", [])
                token = r.get("nextPageToken")
                if not token:
                    return righe
        except RuntimeError as e:
            if versione == "v1beta" or "404" not in str(e):
                raise
            righe, token = [], None
    return righe


def registra_merchant(g):
    """Registra una volta il progetto Google Cloud presso Merchant Center (richiesto dalla Merchant API).
    Serve che in quel momento l'account di servizio sia Amministratore di Merchant Center.
    Come email del contatto tecnico usa DEVELOPER_EMAIL se impostata, altrimenti l'account di servizio."""
    url = (f"https://merchantapi.googleapis.com/accounts/v1/accounts/{MERCHANT_ID}"
           "/developerRegistration:registerGcp")
    email = os.environ.get("DEVELOPER_EMAIL", "").strip() or g.cred.service_account_email
    return g.chiama("POST", url, {"developerEmail": email})


def merchant(g):
    out = {}
    try:
        merchant_query(g, "SELECT offer_id FROM product_view LIMIT 1")
    except RuntimeError as e:
        if "not registered" in str(e):
            print("Merchant: progetto non registrato, provo a registrarlo...")
            print("registerGcp:", str(registra_merchant(g))[:300])
        else:
            raise
    prodotti = merchant_query(g, "SELECT offer_id, title, aggregated_reporting_context_status, item_issues "
                                 "FROM product_view")
    stati = Counter()
    problemi = Counter()
    for r in prodotti:
        pv = r.get("productView", {})
        stati[pv.get("aggregatedReportingContextStatus", "SCONOSCIUTO")] += 1
        for issue in pv.get("itemIssues", []) or []:
            problemi[(issue.get("type") or {}).get("code") or str(issue)[:60]] += 1
    out["prodotti_totali"] = len(prodotti)
    out["stati"] = dict(stati)
    out["problemi_principali"] = dict(problemi.most_common(8))
    fine = date.today()
    inizio = fine - timedelta(days=28)
    perf = merchant_query(g, "SELECT date, clicks, impressions FROM product_performance_view "
                             f"WHERE date BETWEEN '{inizio.isoformat()}' AND '{fine.isoformat()}'")
    per_giorno = {}
    for r in perf:
        p = r.get("productPerformanceView", {})
        d = p.get("date") or {}
        chiave = f"{d.get('year')}-{int(d.get('month', 0)):02d}-{int(d.get('day', 0)):02d}"
        x = per_giorno.setdefault(chiave, {"clic": 0, "impressioni": 0})
        x["clic"] += int(p.get("clicks") or 0)
        x["impressioni"] += int(p.get("impressions") or 0)
    out["prestazioni_per_giorno"] = dict(sorted(per_giorno.items()))
    out["clic_28g"] = sum(v["clic"] for v in per_giorno.values())
    out["impressioni_28g"] = sum(v["impressioni"] for v in per_giorno.values())
    return out


def main():
    chiave = os.environ.get("GOOGLE_SA_KEY", "").strip()
    if not chiave:
        print("Segreto GOOGLE_SA_KEY mancante: statistiche Google saltate.")
        return 0
    os.makedirs(CARTELLA, exist_ok=True)
    g = Google(chiave)
    dati = {"aggiornato": datetime.now(ROMA).isoformat(timespec="minutes"), "avvisi": []}
    for nome, funzione in (("analytics", analytics), ("search_console", search_console), ("merchant", merchant)):
        try:
            dati[nome] = funzione(g)
            print(f"{nome}: ok")
        except Exception as e:  # ogni servizio è indipendente
            dati[nome] = None
            dati["avvisi"].append(f"{nome}: {str(e)[:300]}")
            print(f"{nome}: ERRORE {e}")

    with open(os.path.join(CARTELLA, "google.json"), "w", encoding="utf-8") as f:
        json.dump(dati, f, indent=1, ensure_ascii=False)
        f.write("\n")

    # storico: una riga al giorno
    oggi = datetime.now(ROMA).strftime("%Y-%m-%d")
    ga = dati.get("analytics") or {}
    ieri = (datetime.now(ROMA) - timedelta(days=1)).strftime("%Y%m%d")
    ga_ieri = next((r for r in ga.get("per_giorno", []) if r.get("date") == ieri), {})
    sc = (dati.get("search_console") or {}).get("totale_28g") or {}
    mc = dati.get("merchant") or {}
    riga = [oggi, ga_ieri.get("activeUsers", ""), ga_ieri.get("sessions", ""),
            sc.get("clicks", ""), sc.get("impressions", ""),
            mc.get("prodotti_totali", ""), json.dumps(mc.get("stati", {}), ensure_ascii=False),
            mc.get("clic_28g", ""), mc.get("impressioni_28g", "")]
    storico = os.path.join(CARTELLA, "storico_google.csv")
    righe = []
    if os.path.exists(storico):
        with open(storico, encoding="utf-8") as f:
            righe = [r for r in csv.reader(f)][1:]
    righe = [r for r in righe if r and r[0] != oggi] + [riga]
    with open(storico, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["data", "ga_utenti_ieri", "ga_sessioni_ieri", "sc_clic_28g", "sc_impressioni_28g",
                    "merchant_prodotti", "merchant_stati", "merchant_clic_28g", "merchant_impressioni_28g"])
        w.writerows(righe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
