# IndoBot — Panduan Deploy ke Railway

## Cara Deploy (Step by Step)

### Step 1 — Daftar GitHub (gratis)
1. Buka github.com
2. Klik Sign Up
3. Daftar pakai email

### Step 2 — Upload file ke GitHub
1. Setelah login, klik tombol "+" pojok kanan atas
2. Pilih "New repository"
3. Nama: indobot
4. Pilih "Public"
5. Klik "Create repository"
6. Klik "Upload files"
7. Upload semua file (bot.py, requirements.txt, Procfile)
8. Klik "Commit changes"

### Step 3 — Daftar Railway (gratis)
1. Buka railway.app
2. Klik "Start a New Project"
3. Login pakai GitHub

### Step 4 — Deploy bot
1. Klik "Deploy from GitHub repo"
2. Pilih repository "indobot"
3. Railway otomatis detect dan deploy!

### Step 5 — Set Environment Variables (PENTING!)
Di Railway, klik project → Settings → Variables → Add:

| Variable | Value |
|----------|-------|
| INDODAX_API_KEY | API Key Indodax kamu |
| INDODAX_SECRET_KEY | Secret Key Indodax kamu |
| TELEGRAM_TOKEN | Token dari @BotFather |
| TELEGRAM_CHAT_ID | Chat ID dari @userinfobot |
| MODAL | 100000 |
| TARGET_PROFIT | 20000 |
| STOP_LOSS_PCT | 3 |

### Step 6 — Jalankan!
1. Klik "Deploy"
2. Bot langsung jalan 24 jam di cloud!
3. Pantau notif Telegram di HP kamu

## Selesai! Bot jalan 24 jam tanpa PC & HP 🚀
