#!/usr/bin/env python3
"""
Piccoli Ruggiti - statistiche giornaliere di Instagram e Facebook via Graph API.
Scrive statistiche/ultimo.json (fotografia di oggi) e aggiunge una riga a
statistiche/storico.csv (follower giorno per giorno). Non pubblica nulla.

Con i soli permessi attuali legge follower, numero di post, like e commenti.
Copertura/visualizzazioni richiedono anche instagram_manage_insights e
read_insights: se mancano, lo script lo segnala in "avvisi" e va avanti.
"""
import csv
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from pubblica import ErroreMeta, chiama, info_account

ROMA = ZoneInfo("Europe/Rome")
QUI = os.path.dirname(os.path.abspath(__file__))
CARTELLA = os.path.join(QUI, "statistiche")


def prova(avvisi, nome, funzione):
    try:
        return funzione()
    except ErroreMeta as e:
        avvisi.append(f"{nome}: {e}")
        return None


def main():
    os.makedirs(CARTELLA, exist_ok=True)
    adesso = datetime.now(ROMA)
    avvisi = []
    page_id, nome, ig_id, ig_user = info_account()

    # ---- Facebook
    pagina = prova(avvisi, "pagina", lambda: chiama("GET", page_id, fields="name,followers_count,fan_count")) or {}
    post_fb = prova(avvisi, "post facebook", lambda: chiama(
        "GET", f"{page_id}/posts", limit=15,
        fields="id,created_time,message,permalink_url,shares,"
               "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)")) or {}
    fb_post = []
    for p in post_fb.get("data", []):
        fb_post.append({
            "data": p.get("created_time"),
            "testo": (p.get("message") or "")[:80],
            "reazioni": p.get("reactions", {}).get("summary", {}).get("total_count"),
            "commenti": p.get("comments", {}).get("summary", {}).get("total_count"),
            "condivisioni": (p.get("shares") or {}).get("count", 0),
            "link": p.get("permalink_url"),
        })
    fb_insight = prova(avvisi, "insights pagina (serve read_insights)", lambda: chiama(
        "GET", f"{page_id}/insights", metric="page_impressions_unique,page_post_engagements", period="day"))

    # ---- Instagram
    profilo = {}
    ig_post = []
    if ig_id:
        profilo = prova(avvisi, "profilo instagram", lambda: chiama(
            "GET", ig_id, fields="username,followers_count,follows_count,media_count")) or {}
        media = prova(avvisi, "post instagram", lambda: chiama(
            "GET", f"{ig_id}/media", limit=15,
            fields="id,caption,media_type,media_product_type,timestamp,like_count,comments_count,permalink")) or {}
        insight_ok = True
        for m in media.get("data", []):
            voce = {
                "data": m.get("timestamp"),
                "tipo": m.get("media_product_type") or m.get("media_type"),
                "testo": (m.get("caption") or "")[:80],
                "like": m.get("like_count"),
                "commenti": m.get("comments_count"),
                "link": m.get("permalink"),
            }
            if insight_ok:
                try:
                    ins = chiama("GET", f"{m['id']}/insights", metric="reach,saved,shares,views")
                    for x in ins.get("data", []):
                        voce[x["name"]] = (x.get("values") or [{}])[0].get("value", x.get("total_value", {}).get("value"))
                except ErroreMeta as e:
                    insight_ok = False
                    avvisi.append(f"insights instagram (serve instagram_manage_insights): {e}")
            ig_post.append(voce)
    else:
        avvisi.append("nessun account Instagram collegato alla Pagina")

    dati = {
        "aggiornato": adesso.isoformat(timespec="minutes"),
        "facebook": {
            "pagina": pagina.get("name") or nome,
            "follower": pagina.get("followers_count"),
            "mi_piace": pagina.get("fan_count"),
            "insights": fb_insight.get("data") if fb_insight else None,
            "post": fb_post,
        },
        "instagram": {
            "account": profilo.get("username") or ig_user,
            "follower": profilo.get("followers_count"),
            "seguiti": profilo.get("follows_count"),
            "post_totali": profilo.get("media_count"),
            "post": ig_post,
        },
        "avvisi": avvisi,
    }
    with open(os.path.join(CARTELLA, "ultimo.json"), "w", encoding="utf-8") as f:
        json.dump(dati, f, indent=1, ensure_ascii=False)
        f.write("\n")

    storico = os.path.join(CARTELLA, "storico.csv")
    nuovo = not os.path.exists(storico)
    righe = []
    if not nuovo:
        with open(storico, encoding="utf-8") as f:
            righe = [r for r in csv.reader(f)][1:]
    oggi = adesso.strftime("%Y-%m-%d")
    righe = [r for r in righe if r and r[0] != oggi]
    righe.append([oggi, dati["instagram"]["follower"], dati["instagram"]["post_totali"],
                  dati["facebook"]["follower"], dati["facebook"]["mi_piace"],
                  sum((p.get("like") or 0) for p in ig_post),
                  sum((p.get("reazioni") or 0) for p in fb_post)])
    with open(storico, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["data", "ig_follower", "ig_post", "fb_follower", "fb_mi_piace",
                    "ig_like_ultimi_post", "fb_reazioni_ultimi_post"])
        w.writerows(righe)

    print(f"Instagram @{dati['instagram']['account']}: {dati['instagram']['follower']} follower, "
          f"{dati['instagram']['post_totali']} post")
    print(f"Facebook {dati['facebook']['pagina']}: {dati['facebook']['follower']} follower")
    for a in avvisi:
        print("AVVISO:", a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
