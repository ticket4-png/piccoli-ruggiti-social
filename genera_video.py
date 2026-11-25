"""Genera clip video con Veo (Vertex AI) per i Reel di Piccoli Ruggiti.

Legge video_richieste.json:
  [{"id": "leoncino-ruggito", "prompt": "...", "immagine": "media/....png" (facoltativa),
    "modello": "veo-3.1-fast-generate-001" (facoltativo), "secondi": 8, "audio": true}]
Per ogni richiesta non ancora fatta (vedi video_stato.json) genera UNA clip verticale 9:16 e la salva
in media/generati/<id>.mp4. Le clip poi si montano nel Reel finale (ffmpeg) e si mettono nel calendario.

Protezioni di spesa:
  - MAX_CLIP_PER_ESECUZIONE clip per esecuzione;
  - BUDGET_SECONDI_MESE secondi generati al mese (conteggio in video_stato.json);
  - una richiesta fallita non viene ritentata automaticamente (va rimessa a mano).
Credenziali: segreto GitHub GOOGLE_SA_KEY (service account del progetto piccoli-ruggiti-statistiche,
ruolo "Agent Platform User").
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

QUI = os.path.dirname(os.path.abspath(__file__))
RICHIESTE = os.path.join(QUI, "video_richieste.json")
STATO = os.path.join(QUI, "video_stato.json")
USCITA = os.path.join(QUI, "media", "generati")
REGIONE = os.environ.get("VEO_REGIONE", "us-central1")
MODELLO_DEFAULT = os.environ.get("VEO_MODELLO", "veo-3.1-fast-generate-001")
MAX_CLIP_PER_ESECUZIONE = int(os.environ.get("MAX_CLIP_PER_ESECUZIONE", "3"))
BUDGET_SECONDI_MESE = int(os.environ.get("BUDGET_SECONDI_MESE", "120"))


def carica(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def salva(p, dati):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(dati, f, indent=1, ensure_ascii=False)
        f.write("\n")


def credenziali():
    info = json.loads(os.environ["GOOGLE_SA_KEY"])
    cred = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    cred.refresh(Request())
    return cred, info["project_id"]


def genera(cred, progetto, r):
    modello = r.get("modello") or MODELLO_DEFAULT
    base = (f"https://{REGIONE}-aiplatform.googleapis.com/v1/projects/{progetto}/locations/{REGIONE}"
            f"/publishers/google/models/{modello}")
    istanza = {"prompt": r["prompt"]}
    if r.get("immagine"):
        percorso = os.path.join(QUI, r["immagine"])
        mime = "image/png" if percorso.lower().endswith(".png") else "image/jpeg"
        with open(percorso, "rb") as f:
            istanza["image"] = {"bytesBase64Encoded": base64.b64encode(f.read()).decode(), "mimeType": mime}
    parametri = {
        "aspectRatio": "9:16",
        "durationSeconds": int(r.get("secondi", 8)),
        "generateAudio": bool(r.get("audio", True)),
        "sampleCount": 1,
        "resolution": r.get("risoluzione", "720p"),
        "personGeneration": r.get("persone", "dont_allow"),
    }
    h = {"Authorization": f"Bearer {cred.token}", "Content-Type": "application/json"}
    resp = requests.post(base + ":predictLongRunning", headers=h,
                         json={"instances": [istanza], "parameters": parametri}, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"avvio {resp.status_code}: {resp.text[:500]}")
    operazione = resp.json()["name"]
    print(f"  operazione avviata: {operazione.split('/')[-1]}", flush=True)
    for _ in range(90):  # fino a ~15 minuti
        time.sleep(10)
        if not cred.valid:
            cred.refresh(Request())
            h["Authorization"] = f"Bearer {cred.token}"
        st = requests.post(base + ":fetchPredictOperation", headers=h,
                           json={"operationName": operazione}, timeout=120)
        if st.status_code != 200:
            raise RuntimeError(f"stato {st.status_code}: {st.text[:500]}")
        j = st.json()
        if j.get("done"):
            if "error" in j:
                raise RuntimeError(f"errore Veo: {json.dumps(j['error'])[:500]}")
            ris = j.get("response", {})
            video = (ris.get("videos") or [{}])[0]
            if ris.get("raiMediaFilteredCount"):
                raise RuntimeError(f"bloccato dai filtri: {ris.get('raiMediaFilteredReasons')}")
            if "bytesBase64Encoded" not in video:
                raise RuntimeError(f"risposta senza video: {json.dumps(ris)[:500]}")
            return base64.b64decode(video["bytesBase64Encoded"]), modello, parametri["durationSeconds"]
    raise RuntimeError("tempo scaduto in attesa del video")


def main():
    richieste = carica(RICHIESTE, [])
    stato = carica(STATO, {"mesi": {}, "clip": {}})
    mese = datetime.now(timezone.utc).strftime("%Y-%m")
    usati = stato["mesi"].get(mese, 0)
    da_fare = [r for r in richieste if r["id"] not in stato["clip"]]
    if not da_fare:
        print("Nessuna richiesta nuova.")
        return 0
    if "--verifica" in sys.argv:
        cred, progetto = credenziali()
        print(f"Credenziali OK, progetto {progetto}. Richieste da fare: {[r['id'] for r in da_fare]}; "
              f"secondi usati questo mese {usati}/{BUDGET_SECONDI_MESE}.")
        return 0
    cred, progetto = credenziali()
    os.makedirs(USCITA, exist_ok=True)
    fatte = 0
    for r in da_fare:
        sec = int(r.get("secondi", 8))
        if fatte >= MAX_CLIP_PER_ESECUZIONE:
            print("Raggiunto il massimo di clip per esecuzione: il resto alla prossima.")
            break
        if usati + sec > BUDGET_SECONDI_MESE:
            print(f"STOP: budget mensile ({BUDGET_SECONDI_MESE} s) raggiunto, {r['id']} non generata.")
            break
        print(f"Genero {r['id']} ({sec} s)...", flush=True)
        try:
            dati, modello, durata = genera(cred, progetto, r)
        except Exception as e:  # noqa: BLE001
            stato["clip"][r["id"]] = {"esito": "errore", "errore": str(e)[:600],
                                      "alle": datetime.now(timezone.utc).isoformat(timespec="minutes")}
            print(f"ERRORE {r['id']}: {e}")
            salva(STATO, stato)
            continue
        nome = os.path.join("media", "generati", f"{r['id']}.mp4")
        with open(os.path.join(QUI, nome), "wb") as f:
            f.write(dati)
        usati += durata
        stato["mesi"][mese] = usati
        stato["clip"][r["id"]] = {"esito": "ok", "file": nome, "modello": modello, "secondi": durata,
                                  "alle": datetime.now(timezone.utc).isoformat(timespec="minutes")}
        salva(STATO, stato)
        fatte += 1
        print(f"OK {r['id']} -> {nome} ({len(dati)//1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
