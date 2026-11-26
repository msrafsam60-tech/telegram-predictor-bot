# 🤖 BA VC JOINER — Telegram Live & Channel Joiner Bot

## ✅ Features
- Multiple Telegram account login (Phone + OTP + 2FA via Telethon)
- **🔴 JOIN LIVE**: First joins target group/channel with all accounts, shows joined count, asks how many accounts to join VC (`1-N`), and joins with live progress bar (`▬ 100% 🎉`)
- **🟢 JOIN CH/GRP**: Bulk join public or private Channels/Groups with selected number of accounts
- **➕ ADD ACCOUNT / 🗄️ ALL ACCOUNT**: Add, view, and remove accounts anytime
- **➕ ADD ADMIN / 🗄️ ALL ADMIN**: Interactive Owner-only admin management directly from menu buttons
- **📥 BOT IMPORT / 📤 BOT EXPORT**: Backup & restore accounts via `.json` file
- **✋ Hand Raise All & 🔴 Leave Live**: Inline controls right on the Live Success card (plus `/raise` and `/leave`)
- Auto-rejoin watchdog (every 4 seconds) & auto-mute protection
- 24/7 Render + Cron-job.org keep-alive health server

---

## 🚀 Deploy on Render (Step by Step)

### Step 1 — GitHub-এ Upload করুন
1. GitHub-এ একটি নতুন **Private repo** তৈরি করুন
2. এই ফাইলগুলো upload করুন:
   - `bot.py`
   - `requirements.txt`
   - `render.yaml`
   - `nixpacks.toml`
   - `README.md`

### Step 2 — Render-এ Deploy করুন
1. [render.com](https://render.com) এ যান → **New +** → **Web Service**
2. **GitHub repo** connect করুন
3. Settings:
   - **Name:** `ba-vc-joiner-bot`
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Plan:** Free

### Step 3 — Environment Variables যোগ করুন
Render dashboard → **Environment** tab:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | @BotFather থেকে পাওয়া token |
| `OWNER_ID` | আপনার Telegram User ID |
| `API_ID` | my.telegram.org থেকে |
| `API_HASH` | my.telegram.org থেকে |

### Step 4 — Deploy করুন
**Deploy** চাপুন। Deploy হলে একটি URL পাবেন:
```
https://ba-vc-joiner-bot.onrender.com
```

---

## ⏰ Cron-job.org দিয়ে 24/7 Alive রাখুন

1. [cron-job.org](https://cron-job.org) এ **Free account** খুলুন
2. **Create Cronjob** চাপুন
3. **URL**: `https://YOUR-APP-NAME.onrender.com/health`
4. **Schedule**: Every 5 minutes (`GET`)

---

## 🤖 Bot Commands & Menu

```
/start         → Main menu
/add user_id   → Admin যোগ করুন (owner only)
/rm user_id    → Admin সরান (owner only)
/admins        → Admin list দেখুন
/raise         → Hand Raise All
/leave         → Leave Live
/cancel        → Cancel current action
```

### Main Menu Buttons (4×2 Coral-Red Grid):
- **🔴 JOIN LIVE** | **🟢 JOIN CH/GRP**
- **➕ ADD ACCOUNT** | **🗄️ ALL ACCOUNT**
- **➕ ADD ADMIN** | **🗄️ ALL ADMIN**
- **📥 BOT IMPORT** | **📤 BOT EXPORT**
