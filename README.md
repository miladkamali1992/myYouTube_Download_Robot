<p align="center">
  <img src="cover.png" alt="Cover" width="100%">
</p>

# myYouTube Download Robot

A Telegram bot that downloads YouTube videos and music. Send a link, pick a quality, get the file in your chat.

## 🚀 Features

- Download videos in the quality you want — from 144p up to 4K, whatever the video actually has
- Download the audio alone as MP3
- Files converted earlier — by you or by other users — are sent instantly

## 📦 Requirements

- Python 3.12
- PostgreSQL 15
- ffmpeg and Deno (already included in `tools/`)

## ▶️ Setup

```powershell
git clone https://github.com/miladkamali1992/myYouTube_Download_Robot.git
cd myYouTube_Download_Robot
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather), and `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org).
Put your Telegram user ID in `ADMIN_USER_IDS` and leave `TEST_MODE=1` while you're testing; set it to `0` when the bot is open to everyone.

## ▶️ Run

```powershell
venv\Scripts\python.exe bot.py
# or
run.bat
```

## ⚠️ Notes

- `.env` holds your tokens — it's in `.gitignore` and must never be committed.
- Telegram limits uploads to 2 GB, bigger files are rejected.

## 📄 License

MIT

> ⚖️ Only download content you have the right to download.

---

## 👨‍💻 Author
**Milad**  
📬 Telegram: **https://MiladKamali.t.me**
