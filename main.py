import asyncio
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
import yt_dlp

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- 1. COOKIE SETUP ---
COOKIE_PATH = "/tmp/cookies.txt"
YT_COOKIES_STR = os.getenv("YT_COOKIES")

def get_ydl_opts(extra_opts=None):
    if YT_COOKIES_STR:
        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            f.write(YT_COOKIES_STR.replace('\\n', '\n').strip('"').strip("'"))
    
    opts = {
        'cookiefile': COOKIE_PATH if YT_COOKIES_STR else None,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        # This is the "Magic" fix for 400 errors on Cloud Servers
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    if extra_opts:
        opts.update(extra_opts)
    return opts

# --- 2. LOGIC ---
TEMP_DIR = Path(tempfile.gettempdir()) / "ytdlp_serve"
TEMP_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/info")
async def get_info(url: str):
    try:
        loop = asyncio.get_event_loop()
        # Using the library directly instead of subprocess
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            
        formats = data.get("formats", [])
        video_formats, audio_formats = [], []
        seen_video, seen_audio = set(), set()

        for f in reversed(formats):
            if f.get("vcodec") != "none" and f.get("height") and f["height"] not in seen_video:
                seen_video.add(f["height"])
                video_formats.append({"id": f["format_id"], "label": f"{f['height']}p ({f['ext']})", "height": f["height"]})
            if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("abr") and round(f["abr"]) not in seen_audio:
                seen_audio.add(round(f["abr"]))
                audio_formats.append({"id": f["format_id"], "label": f"{round(f['abr'])}kbps ({f['ext']})", "abr": f["abr"]})

        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration_string"),
            "uploader": data.get("uploader"),
            "video_formats": sorted(video_formats, key=lambda x: -x["height"]),
            "audio_formats": sorted(audio_formats, key=lambda x: -x["abr"]),
        }
    except Exception as e:
        # This will now show the REAL error on your phone screen
        return JSONResponse({"error": str(e)}, status_code=400)

@app.websocket("/ws/download")
async def download_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        url = data.get("url")
        job_id = str(uuid.uuid4())
        job_dir = TEMP_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        
        def progress_hook(d):
            if d['status'] == 'downloading':
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "progress", "percent": d.get('downloaded_bytes', 0) / d.get('total_bytes', 1) * 100}),
                    asyncio.get_event_loop()
                )

        ydl_opts = get_ydl_opts({
            'format': f"{data.get('format_id')}+bestaudio/best" if not data.get('audio_only') else 'bestaudio',
            'outtmpl': str(job_dir / "%(title)s.%(ext)s"),
            'progress_hooks': [progress_hook],
            'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3'} if data.get('audio_only') else {'key': 'FFmpegVideoConvertor','preferredformat': 'mp4'}],
        })

        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            await loop.run_in_executor(None, lambda: ydl.download([url]))

        files = list(job_dir.iterdir())
        await websocket.send_json({"type": "done", "file_id": job_id, "filename": files[0].name})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})

@app.get("/download-file/{file_id}")
async def serve_file(file_id: str):
    job_dir = TEMP_DIR / file_id
    files = list(job_dir.iterdir()) if job_dir.exists() else []
    if not files: return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(path=str(files[0]), filename=files[0].name)