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

# --- COOKIE HANDLING ---
# This reads the cookies from a Render Environment Variable named "YT_COOKIES"
YT_COOKIES_CONTENT = os.getenv("YT_COOKIES")
if YT_COOKIES_CONTENT:
    with open("cookies.txt", "w") as f:
        f.write(YT_COOKIES_CONTENT)

import sys as _sys
import shutil as _shutil2
# Improved path finding for yt-dlp
YT_DLP = _shutil2.which("yt-dlp") or "yt-dlp"

TEMP_DIR = Path(tempfile.gettempdir()) / "ytdlp_serve"
TEMP_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/info")
async def get_info(url: str):
    try:
        # Added --cookies cookies.txt to the command
        cmd = [YT_DLP, "--cookies", "cookies.txt", "-J", "--no-playlist", url]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr or "Failed to fetch info"}, status_code=400)

        data = json.loads(result.stdout)
        formats = data.get("formats", [])
        video_formats, audio_formats = [], []
        seen_video, seen_audio = set(), set()

        for f in reversed(formats):
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height")
            abr = f.get("abr")
            fid = f.get("format_id")
            ext = f.get("ext", "")

            if vcodec != "none" and height and height not in seen_video:
                seen_video.add(height)
                video_formats.append({"id": fid, "label": f"{height}p ({ext})", "height": height})

            if acodec != "none" and vcodec == "none" and abr and round(abr) not in seen_audio:
                seen_audio.add(round(abr))
                audio_formats.append({"id": fid, "label": f"{round(abr)}kbps ({ext})", "abr": abr})

        subtitles = {}
        for lang, subs in {**data.get("subtitles", {}), **data.get("automatic_captions", {})}.items():
            if subs:
                subtitles[lang] = lang

        return {
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration_string"),
            "uploader": data.get("uploader"),
            "video_formats": sorted(video_formats, key=lambda x: -x["height"]),
            "audio_formats": sorted(audio_formats, key=lambda x: -x["abr"]),
            "subtitles": subtitles,
        }

    except subprocess.TimeoutExpired:
        return {"error": "Timed out fetching info"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/download-file/{file_id}")
async def serve_file(file_id: str):
    if not re.match(r'^[a-f0-9\-]+$', file_id):
        return JSONResponse({"error": "Invalid file id"}, status_code=400)

    job_dir = TEMP_DIR / file_id
    if not job_dir.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    files = list(job_dir.iterdir())
    if not files:
        return JSONResponse({"error": "No file found"}, status_code=404)

    filepath = files[0]

    async def cleanup():
        await asyncio.sleep(10)
        shutil.rmtree(job_dir, ignore_errors=True)

    asyncio.create_task(cleanup())

    return FileResponse(path=str(filepath), filename=filepath.name)

@app.websocket("/ws/download")
async def download_ws(websocket: WebSocket):
    await websocket.accept()
    job_dir = None
    try:
        data = await websocket.receive_json()
        url = data.get("url")
        format_id = data.get("format_id")
        subtitle_lang = data.get("subtitle_lang")
        audio_only = data.get("audio_only", False)
        subtitle_only = data.get("subtitle_only", False)

        job_id = str(uuid.uuid4())
        job_dir = TEMP_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        output_template = str(job_dir / "%(title)s.%(ext)s")

        # Added --cookies cookies.txt to the WebSocket command
        cmd = [YT_DLP, "--cookies", "cookies.txt", "--no-playlist", "--newline", "-o", output_template, url]

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
            stderr=asyncio.subprocess.STDOUT,
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
            filename = files[0].name if files else "download"
            await websocket.send_json({"type": "done", "file_id": job_id, "filename": filename})
        else:
            await websocket.send_json({"type": "error", "message": "Download failed."})

    except WebSocketDisconnect:
        if job_dir: shutil.rmtree(job_dir, ignore_errors=True)
    except Exception as e:
        try: await websocket.send_json({"type": "error", "message": str(e)})
        except: pass