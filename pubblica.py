#!/usr/bin/env python3
"""
Piccoli Ruggiti - pubblicazione automatica di post e storie su Instagram e Facebook
tramite le API ufficiali di Meta (Graph API). Gira su GitHub Actions.

Legge calendario.json, pubblica i contenuti arrivati all'orario previsto e
annota l'esito in stato.json (che il workflow salva nel repository).

Variabili d'ambiente (segreti GitHub):
  META_PAGE_TOKEN   token della Pagina Facebook "Piccoli Ruggiti" (senza scadenza)
  META_PAGE_ID      (facoltativo) ID della Pagina; se manca viene letto dal token
  GRAPH_VERSION     (facoltativo) versione Graph API, predefinita v27.0
  MEDIA_BASE_URL    URL pubblico della cartella media/ (impostato dal workflow)
Modalità:
  python pubblica.py            -> pubblica i contenuti scaduti
  python pubblica.py --verifica -> controlla token, pagina, account Instagram e calendario, non pubblica nulla
  python pubblica.py --continua -> pubblica i contenuti scaduti, poi resta in attesa (fino a ~5 h 20 min)
                                   e pubblica i successivi all'orario esatto; salva stato.json con git dopo
                                   ogni pubblicazione. Il workflow poi rilancia se stesso, così non dipende
                                   dal cron di GitHub (che a volte parte solo ogni 7-8 ore).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ROMA = ZoneInfo("Europe/Rome")
QUI = os.path.dirname(os.path.abspath(__file__))
CALENDARIO = os.path.join(QUI, "calendario.json")
STATO = os.path.join(QUI, "stato.json")
MEDIA_DIR = os.path.join(QUI, "media")

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v27.0").strip() or "v27.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
TOKEN = os.environ.get("META_PAGE_TOKEN", "").strip()
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")

RITARDO_MASSIMO = timedelta(hours=12)  # GitHub lancia il cron anche ogni 7-8 h; oltre questo ritardo un contenuto non viene più pubblicato
TENTATIVI_MASSIMI = 3                  # tentativi (uno per esecuzione) prima di arrendersi
DURATA_MASSIMA = timedelta(hours=5, minutes=20)  # modalità --continua: GitHub ferma un job dopo 6 h


class ErroreMeta(Exception):
    pass


# ---------------------------------------------------------------- Graph API
def chiama(metodo, percorso, **params):
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{percorso.lstrip('/')}"
    for prova in range(3):
        try:
            if metodo == "GET":
                r = requests.get(url, params=params, timeout=60)
            else:
                r = requests.post(url, data=params, timeout=120)
        except requests.RequestException as e:
            if prova == 2:
                raise ErroreMeta(f"rete: {e}")
            time.sleep(5)
            continue
        try:
            dati = r.json()
        except ValueError:
            dati = {"error": {"message": r.text[:300]}}
        if r.status_code >= 500 and prova < 2:
            time.sleep(5)
            continue
        if "error" in dati:
            err = dati["error"]
            raise ErroreMeta(f"{err.get('message')} (codice {err.get('code')}/{err.get('error_subcode')})")
        return dati
    raise ErroreMeta("nessuna risposta")


def info_account():
    """Restituisce (page_id, nome_pagina, ig_id, ig_username)."""
    page_id = os.environ.get("META_PAGE_ID", "").strip()
    if page_id:
        p = chiama("GET", page_id, fields="id,name,instagram_business_account{id,username}")
    else:
        p = chiama("GET", "me", fields="id,name,instagram_business_account{id,username}")
    ig = p.get("instagram_business_account") or {}
    return p["id"], p.get("name"), ig.get("id"), ig.get("username")


# ---------------------------------------------------------------- Instagram
def ig_attendi(container_id, secondi=120):
    fine = time.time() + secondi
    while time.time() < fine:
        s = chiama("GET", container_id, fields="status_code,status")
        codice = s.get("status_code")
        if codice == "FINISHED":
            return
        if codice in ("ERROR", "EXPIRED"):
            raise ErroreMeta(f"Instagram ha rifiutato il file: {s.get('status')}")
        time.sleep(4)
    raise ErroreMeta("Instagram non ha finito di elaborare il file in tempo")


def ig_post(ig_id, urls, testo):
    if len(urls) == 1:
        c = chiama("POST", f"{ig_id}/media", image_url=urls[0], caption=testo)["id"]
    else:
        figli = []
        for u in urls[:10]:
            f = chiama("POST", f"{ig_id}/media", image_url=u, is_carousel_item="true")["id"]
            ig_attendi(f)
            figli.append(f)
        c = chiama("POST", f"{ig_id}/media", media_type="CAROUSEL",
                   children=",".join(figli), caption=testo)["id"]
    ig_attendi(c)
    return chiama("POST", f"{ig_id}/media_publish", creation_id=c)["id"]


def ig_reel(ig_id, url, testo):
    c = chiama("POST", f"{ig_id}/media", media_type="REELS", video_url=url,
               caption=testo, share_to_feed="true")["id"]
    ig_attendi(c, secondi=600)
    return chiama("POST", f"{ig_id}/media_publish", creation_id=c)["id"]


def ig_storia(ig_id, url):
    if url.lower().endswith(".mp4"):
        c = chiama("POST", f"{ig_id}/media", video_url=url, media_type="STORIES")["id"]
        ig_attendi(c, secondi=600)
    else:
        c = chiama("POST", f"{ig_id}/media", image_url=url, media_type="STORIES")["id"]
        ig_attendi(c)
    return chiama("POST", f"{ig_id}/media_publish", creation_id=c)["id"]


# ---------------------------------------------------------------- Facebook
def fb_foto_nascosta(page_id, url):
    return chiama("POST", f"{page_id}/photos", url=url, published="false")["id"]


def fb_post(page_id, urls, testo):
    if len(urls) == 1:
        return chiama("POST", f"{page_id}/photos", url=urls[0], message=testo)["id"]
    params = {"message": testo}
    for i, u in enumerate(urls[:10]):
        params[f"attached_media[{i}]"] = json.dumps({"media_fbid": fb_foto_nascosta(page_id, u)})
    return chiama("POST", f"{page_id}/feed", **params)["id"]


def fb_reel(page_id, url, testo):
    inizio = chiama("POST", f"{page_id}/video_reels", upload_phase="start")
    video_id = inizio["video_id"]
    r = requests.post(f"https://rupload.facebook.com/video-upload/{GRAPH_VERSION}/{video_id}",
                      headers={"Authorization": f"OAuth {TOKEN}", "file_url": url}, timeout=300)
    if r.status_code >= 300:
        raise ErroreMeta(f"caricamento video Facebook: {r.text[:300]}")
    chiama("POST", f"{page_id}/video_reels", upload_phase="finish", video_id=video_id,
           video_state="PUBLISHED", description=testo)
    return video_id


def fb_storia(page_id, url):
    foto = fb_foto_nascosta(page_id, url)
    r = chiama("POST", f"{page_id}/photo_stories", photo_id=foto)
    return r.get("post_id") or r.get("id") or str(r)


# ---------------------------------------------------------------- calendario
def carica(percorso, default):
    if not os.path.exists(percorso):
        return default
    with open(percorso, encoding="utf-8") as f:
        return json.load(f)


def salva_stato(stato):
    with open(STATO, "w", encoding="utf-8") as f:
        json.dump(stato, f, indent=1, ensure_ascii=False)
        f.write("\n")


def orario(voce):
    return datetime.strptime(voce["quando"], "%Y-%m-%d %H:%M").replace(tzinfo=ROMA)


def url_media(nome):
    return f"{MEDIA_BASE_URL}/{nome}"


def controlla_calendario(cal):
    """Errori di forma nel calendario (file mancanti, campi sbagliati)."""
    problemi, visti = [], set()
    for v in cal:
        vid = v.get("id", "?")
        if vid in visti:
            problemi.append(f"{vid}: id duplicato")
        visti.add(vid)
        try:
            orario(v)
        except Exception:
            problemi.append(f"{vid}: 'quando' deve essere AAAA-MM-GG HH:MM")
        if v.get("tipo") not in ("post", "storia", "reel"):
            problemi.append(f"{vid}: 'tipo' deve essere post, storia o reel")
        imm = v.get("immagini") or []
        if not imm:
            problemi.append(f"{vid}: nessuna immagine")
        if v.get("tipo") in ("storia", "reel") and len(imm) != 1:
            problemi.append(f"{vid}: una storia o un reel vuole esattamente 1 file")
        if v.get("tipo") == "reel" and not all(n.lower().endswith(".mp4") for n in imm):
            problemi.append(f"{vid}: il reel deve essere un video .mp4")
        if len(imm) > 10:
            problemi.append(f"{vid}: massimo 10 immagini")
        for n in imm:
            if not n.lower().endswith((".jpg", ".jpeg", ".mp4")):
                problemi.append(f"{vid}: {n} deve essere JPG (o MP4 per reel e storie video)")
            if v.get("tipo") == "post" and n.lower().endswith(".mp4"):
                problemi.append(f"{vid}: i post accettano solo immagini JPG (per i video usa tipo reel)")
            if not os.path.exists(os.path.join(MEDIA_DIR, n)):
                problemi.append(f"{vid}: manca media/{n}")
        for c in v.get("canali", []):
            if c not in ("instagram", "facebook"):
                problemi.append(f"{vid}: canale sconosciuto {c}")
    return problemi


def pubblica_voce(v, canale, page_id, ig_id):
    urls = [url_media(n) for n in v["immagini"]]
    testo = v.get("testo", "")
    if canale == "instagram":
        if not ig_id:
            raise ErroreMeta("nessun account Instagram collegato alla Pagina")
        if v["tipo"] == "reel":
            return ig_reel(ig_id, urls[0], testo)
        return ig_post(ig_id, urls, testo) if v["tipo"] == "post" else ig_storia(ig_id, urls[0])
    if v["tipo"] == "reel":
        return fb_reel(page_id, urls[0], testo)
    return fb_post(page_id, urls, testo) if v["tipo"] == "post" else fb_storia(page_id, urls[0])


def main():
    verifica = "--verifica" in sys.argv
    if not TOKEN:
        print("ERRORE: manca il segreto META_PAGE_TOKEN")
        return 1
    if not MEDIA_BASE_URL:
        print("ERRORE: manca MEDIA_BASE_URL")
        return 1

    cal = carica(CALENDARIO, [])
    stato = carica(STATO, {})
    adesso = datetime.now(ROMA)

    problemi = controlla_calendario(cal)
    for p in problemi:
        print("CALENDARIO:", p)

    page_id, nome, ig_id, ig_user = info_account()
    print(f"Pagina Facebook: {nome} ({page_id})")
    print(f"Instagram: @{ig_user} ({ig_id})" if ig_id else "Instagram: NON collegato")

    if verifica:
        prossimi = [v for v in cal if all(
            stato.get(v["id"], {}).get(c, {}).get("esito") is None for c in v.get("canali", []))]
        prossimi.sort(key=orario)
        print(f"Contenuti in calendario: {len(cal)}, ancora da pubblicare: {len(prossimi)}")
        for v in prossimi[:15]:
            print(f"  {v['quando']}  {v['tipo']:6} {'+'.join(v['canali']):18} {v['id']}")
        if prossimi:
            prova = url_media(prossimi[0]["immagini"][0])
            r = requests.head(prova, timeout=30, allow_redirects=True)
            print(f"Immagine di prova raggiungibile: {r.status_code == 200} ({r.headers.get('content-type')})")
        print("Verifica completata: nessun contenuto pubblicato.")
        return 1 if problemi else 0

    if "--continua" not in sys.argv:
        return passaggio(cal, stato, problemi, page_id, ig_id)

    inizio = datetime.now(ROMA)
    ko = passaggio(cal, stato, problemi, page_id, ig_id)
    salva_git()
    while True:
        aggiorna_git()
        cal = carica(CALENDARIO, [])
        stato = carica(STATO, {})
        problemi = controlla_calendario(cal)
        prossimo = prossima_uscita(cal, stato, problemi)
        if prossimo is None:
            print("Nessun altro contenuto in programma: fine.")
            break
        fine = inizio + DURATA_MASSIMA
        if prossimo > fine:
            resta = (fine - datetime.now(ROMA)).total_seconds()
            print(f"Prossimo contenuto alle {prossimo:%d/%m %H:%M}: attendo fino a fine job ({resta/60:.0f} min), poi rilancio.", flush=True)
            if resta > 0:
                time.sleep(resta)
            break
        attesa = (prossimo - datetime.now(ROMA)).total_seconds()
        if attesa > 0:
            print(f"Attendo fino alle {prossimo:%d/%m %H:%M} ({attesa/60:.0f} min)...", flush=True)
            time.sleep(attesa + 5)
        aggiorna_git()
        cal = carica(CALENDARIO, [])
        stato = carica(STATO, {})
        problemi = controlla_calendario(cal)
        ko += passaggio(cal, stato, problemi, page_id, ig_id)
        salva_git()
    with open(os.path.join(QUI, ".rilancia"), "w") as f:
        f.write("si" if prossima_uscita(cal, stato, problemi) else "no")
    return 1 if ko else 0


def da_fare(v, stato):
    return [c for c in v.get("canali", [])
            if stato.get(v["id"], {}).get(c, {}).get("esito") not in ("pubblicato", "saltato", "fallito")]


def prossima_uscita(cal, stato, problemi):
    """Orario del prossimo contenuto ancora da pubblicare (anche già scaduto, se ha tentativi residui)."""
    esclusi = {p.split(":")[0] for p in problemi}
    orari = [orario(v) for v in cal if v["id"] not in esclusi and da_fare(v, stato)]
    if not orari:
        return None
    adesso = datetime.now(ROMA)
    futuri = [o for o in orari if o > adesso]
    if len(futuri) < len(orari):          # ci sono voci scadute da ritentare: riprova tra 15 minuti
        return adesso + timedelta(minutes=15)
    return min(futuri)


def git(*args):
    return subprocess.run(["git", *args], cwd=QUI, capture_output=True, text=True)


def aggiorna_git():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    git("stash")
    r = git("pull", "--rebase", "-X", "theirs")
    git("stash", "pop")
    if r.returncode:
        print("git pull non riuscito:", r.stderr[:300])


def salva_git():
    if os.environ.get("GITHUB_ACTIONS") != "true" or not git("status", "--porcelain", "stato.json").stdout.strip():
        return
    git("config", "user.name", "piccoli-ruggiti-bot")
    git("config", "user.email", "bot@users.noreply.github.com")
    git("add", "stato.json")
    git("commit", "-m", f"stato pubblicazioni {datetime.now(ROMA):%d/%m %H:%M}")
    for _ in range(3):
        if git("pull", "--rebase", "-X", "theirs").returncode == 0 and git("push").returncode == 0:
            return
        time.sleep(5)
    print("ATTENZIONE: stato.json non salvato su GitHub")


def passaggio(cal, stato, problemi, page_id, ig_id):
    adesso = datetime.now(ROMA)
    ko = 0
    for v in sorted(cal, key=orario):
        if v["id"] in {p.split(":")[0] for p in problemi}:
            continue
        quando = orario(v)
        if quando > adesso:
            continue
        for canale in v["canali"]:
            s = stato.setdefault(v["id"], {}).setdefault(canale, {})
            if s.get("esito") in ("pubblicato", "saltato", "fallito"):
                continue
            if adesso - quando > RITARDO_MASSIMO:
                s.update(esito="saltato", nota=f"in ritardo di oltre {RITARDO_MASSIMO}", alle=adesso.isoformat(timespec="minutes"))
                print(f"SALTATO {v['id']} {canale}: troppo in ritardo")
                salva_stato(stato)
                continue
            try:
                mid = pubblica_voce(v, canale, page_id, ig_id)
                s.clear()
                s.update(esito="pubblicato", id=mid, alle=datetime.now(ROMA).isoformat(timespec="minutes"))
                print(f"OK {v['id']} {canale} -> {mid}")
            except ErroreMeta as e:
                s["tentativi"] = s.get("tentativi", 0) + 1
                s["ultimo_errore"] = str(e)[:400]
                if s["tentativi"] >= TENTATIVI_MASSIMI:
                    s["esito"] = "fallito"
                print(f"ERRORE {v['id']} {canale} (tentativo {s['tentativi']}): {e}")
                ko += 1
            salva_stato(stato)
            time.sleep(3)
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
