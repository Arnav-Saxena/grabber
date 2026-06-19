import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# --- IMPROVED COOKIE HANDLING ---
# This reads from the Render Env Var and ensures it formats correctly
YT_COOKIES_STR = os.getenv("YT_COOKIES")
COOKIE_PATH = "cookies.txt"

if YT_COOKIES_STR:
    # Clean up common formatting issues from copy-pasting into web UIs
    cleaned_cookies = YT_COOKIES_STR.replace('\\n', '\n').strip('"').strip("'")
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write(cleaned_cookies)
    print("LOG: Successfully wrote cookies.txt to disk.")
else:
    print("LOG: No YT_COOKIES environment variable found!")

import sys as _sys
import shutil as _shutil2

# Set binary path based on OS
if _sys.platform == "win32":
    YT_DLP = r"C:\Users\saxen\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\local-packages\Python313\Scripts\yt-dlp.exe"
else:
    # On Render/Linux, 'yt-dlp' should be in the PATH
    YT_DLP = _shutil2.which("yt-dlp") or "yt-dlp"

# Temp dir for downloads
TEMP_DIR = Path(tempfile.gettempdir()) / "ytdlp_serve"
TEMP_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/info")
async def get_info(url: str):
    try:
        # Added User-Agent and Certificates to match a real browser session
        cmd = [
            YT_DLP, 
            "--cookies", COOKIE_PATH, 
            "--no-check-certificates",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "-J", 
            "--no-playlist", 
            url
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            # This prints the specific YouTube error to your Render Logs
            print(f"ERROR from yt-dlp: {result.stderr}")
            return JSONResponse({"error": result.stderr}, status_code=400)

        data = json.loads(result.stdout)
        formats = data.get("formats", [])
        video_formats, audio_formats = [], []
        seen_video, seen_audio = set(), set()

        for f in reversed(formats):
            vcodec, acodec = f.get("vcodec", "none"), f.get("acodec", "none")
            height, abr, fid, ext = f.get("height"), f.get("abr"), f.get("format_id"), f.get("ext", "")

            if vcodec != "none" and height and height not in seen_video:
                seen_video.add(height)
                video_formats.append({"id": fid, "label": f"{height}p ({ext})", "height": height})

            if acodec != "none" and vcodec == "none" and abr and round(abr) not in seen_audio:
                seen_audio.add(round(abr))
                audio_formats.append({"id": fid, "label": f"{round(abr)}kbps ({ext})", "abr": abr})

        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration_string"),
            "uploader": data.get("uploader"),
            "video_formats": sorted(video_formats, key=lambda x: -x["height"]),
            "audio_formats": sorted(audio_formats, key=lambda x: -x["abr"]),
            "subtitles": {lang: lang for lang, subs in {**data.get("subtitles", {}), **data.get("automatic_captions", {})}.items() if subs},
        }
    except Exception as e:
        print(f"CRITICAL EXCEPTION: {str(e)}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/download-file/{file_id}")
async def serve_file(file_id: str):
    if not re.match(r'^[a-f0-9\-]+$', file_id):
        return JSONResponse({"error": "Invalid ID"}, status_code=400)
    job_dir = TEMP_DIR / file_id
    if not job_dir.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    files = list(job_dir.iterdir())
    if not files:
        return JSONResponse({"error": "No file"}, status_code=404)
    
    async def cleanup():
        await asyncio.sleep(30) # Wait 30s to ensure download finishes before deleting
        shutil.rmtree(job_dir, ignore_errors=True)
        
    asyncio.create_task(cleanup())
    return FileResponse(path=str(files[0]), filename=files[0].name)

@app.websocket("/ws/download")
async def download_ws(websocket: WebSocket):
    await websocket.accept()
    job_dir = None
    try:
        data = await websocket.receive_json()
        url, format_id = data.get("url"), data.get("format_id")
        audio_only, subtitle_only, subtitle_lang = data.get("audio_only"), data.get("subtitle_only"), data.get("subtitle_lang")

        job_id = str(uuid.uuid4())
        job_dir = TEMP_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(job_dir / "%(title)s.%(ext)s")

        cmd = [
            YT_DLP, 
            "--cookies", COOKIE_PATH, 
            "--no-check-certificates",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "--newline", 
            "-o", output_template, 
            url
        ]

        if subtitle_only and subtitle_lang:
            cmd += ["--skip-download", "--write-sub", "--write-auto-sub", "--sub-lang", subtitle_lang, "--convert-subs", "srt"]
        elif audio_only:
            cmd += ["-x", "--audio-format", "mp3"]
            if format_id: cmd += ["-f", format_id]
        else:
            if format_id: cmd += ["-f", f"{format_id}+bestaudio/best"]
            else: cmd += ["-f", "bestvideo+bestaudio/best"]
            cmd += ["--merge-output-format", "mp4"]

        proc = await asyncio.create_subprocess_exec(
            *cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.STDOUT
        )

        async for line in proc.stdout:
            line = line.decode("utf-8", errors="replace").strip()
            if not line: continue
            pct_match = re.search(r"(\d+\.\d+)%", line)
            if pct_match:
                await websocket.send_json({"type": "progress", "percent": float(pct_match.group(1)), "line": line})
            else:
                await websocket.send_json({"type": "log", "line": line})

        await proc.wait()
        if proc.returncode == 0:
            files = list(job_dir.iterdir())
            if files:
                await websocket.send_json({"type": "done", "file_id": job_id, "filename": files[0].name})
            else:
                await websocket.send_json({"type": "error", "message": "Download finished but no file found."})
        else:
            await websocket.send_json({"type": "error", "message": "Download failed. check logs."})

    except Exception as e:
        print(f"WS EXCEPTION: {str(e)}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except:
            pass