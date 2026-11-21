# Piccoli Ruggiti – post e storie automatici

Pubblica da solo, agli orari del calendario, post (anche caroselli) e storie sulla
Pagina Facebook **Piccoli Ruggiti** e su Instagram **@piccoliruggiti**, usando le API
ufficiali di Meta. Gira gratis su GitHub Actions ogni 15 minuti: nessun PC acceso,
nessun browser automatico.

## Come funziona
- `media/` — le immagini (JPG; post 1080x1350, storie 1080x1920).
- `calendario.json` — cosa pubblicare e quando (ora italiana):
  ```json
  {
    "id": "2026-11-23-post-black-friday",
    "quando": "2026-11-23 12:45",
    "tipo": "post",
    "canali": ["instagram", "facebook"],
    "immagini": ["bf-1.jpg", "bf-2.jpg"],
    "testo": "Didascalia con emoji e hashtag"
  }
  ```
  `tipo` = `post` (da 1 a 10 immagini JPG: con più immagini diventa un carosello), `storia` (1 immagine JPG o 1 video MP4)
  oppure `reel` (1 video MP4 verticale 1080x1920, 5-90 secondi; su Instagram compare anche nel feed).
- `stato.json` — scritto dal bot: cosa è stato pubblicato, quando, eventuali errori.
  Un contenuto non va mai pubblicato due volte. Se GitHub resta fermo più di 12 ore, il contenuto viene saltato (il 21/11 il cron "ogni 15 minuti" è partito solo 3 volte in 24 h, a 7-8 ore di distanza).
  Dopo 3 tentativi falliti il contenuto viene segnato "fallito" e GitHub ti manda un'email.

## Configurazione (una volta sola)
1. **App Meta**: su developers.facebook.com → Le mie app → Crea app → "Piccoli Ruggiti Publisher".
   Casi d'uso: gestione della Pagina e "Gestisci messaggi e contenuti su Instagram" (API Instagram con login Facebook).
   L'app resta in modalità sviluppo: per le tue Pagine e i tuoi account non serve la revisione di Meta.
2. **Token**: in Strumenti → Graph API Explorer scegli l'app, poi "Get User Access Token" con i permessi
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `pages_manage_engagement`,
   `pages_read_user_content`, `read_insights`, `instagram_basic`, `instagram_content_publish`,
   `instagram_manage_comments`, `instagram_manage_insights`, `business_management`. Nel login seleziona SOLO la Pagina Piccoli Ruggiti
   e l'account Instagram piccoliruggiti.
3. **Token senza scadenza**: copia il token nel Debugger dei token di accesso → "Estendi token di accesso".
   Rimetti il token esteso nel Graph API Explorer ed esegui `me/accounts`: copia l'`access_token` della
   Pagina Piccoli Ruggiti. Ricontrollato nel Debugger, deve risultare "Scade: Mai".
4. **GitHub**: Settings → Secrets and variables → Actions → New repository secret
   `META_PAGE_TOKEN` = il token della Pagina. (Facoltativo: `META_PAGE_ID`.)
5. **Prova**: Actions → "Pubblica post e storie" → Run workflow con "Solo verifica" spuntato.
   Nel log devono comparire la Pagina, @piccoliruggiti e "Immagine di prova raggiungibile: True".

Il token non va mai scritto nei file o nei messaggi: solo nei Secrets di GitHub.

## Statistiche
Il workflow "Statistiche social" gira ogni sera e salva `statistiche/ultimo.json` (follower, like, commenti,
copertura e visualizzazioni dei post Instagram, metriche della Pagina Facebook) e `statistiche/storico.csv`.

### Orari precisi (dal 22/11/2026)
Il cron di GitHub "ogni 15 minuti" in pratica parte anche solo ogni 7-8 ore. Per questo il workflow gira in
modalità `--continua`: pubblica ciò che è scaduto, poi resta acceso (fino a ~5 h 20 min) aspettando l'orario
esatto del contenuto successivo, salva stato.json dopo ogni pubblicazione e alla fine rilancia se stesso
(con il token automatico di GitHub). Quando il calendario non ha più contenuti futuri si ferma; il cron lo
riaccende la volta dopo. Il cron resta come riserva.
