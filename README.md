# yt-dlp grabber

Personal web UI — download YouTube videos, audio, or captions from any browser/phone.

## Deploy on Railway (free, recommended)

1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Pick your repo → Railway auto-detects the Dockerfile and deploys
4. Get your public URL → open on any device

That's it. No config needed.

---

## Run locally (optional)

**Requirements:** Python 3.10+, yt-dlp, ffmpeg

```bash
pip install yt-dlp
brew install ffmpeg        # macOS
# sudo apt install ffmpeg  # Linux

pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000

---

## How it works

- Paste YouTube URL → Fetch
- Pick tab: **Video** / **Audio** / **Captions**
- Select quality → Download to device
- File downloads directly to your browser (phone, tablet, PC — anything)
- Server cleans up the temp file after download

## Notes

- Files are NOT stored permanently on the server — deleted 5s after download
- No accounts, no database, no rate limits — purely personal use
