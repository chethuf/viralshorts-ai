# ViralShorts AI — mobile-first starter

A self-hosted, open-source prototype for turning an authorized source video into multiple vertical Shorts.

## What works in this starter
- Mobile-first web interface accepting a YouTube URL.
- Server-side video retrieval through `yt-dlp` where retrieval is permitted and technically available.
- Speech transcription with Faster-Whisper.
- Candidate clip generation (15–45 seconds), ranked with transparent heuristic scores.
- Vertical 9:16 MP4 rendering with burned-in hook/captions.
- Individual 9:16 thumbnail, title, description, tags and on-screen hook for each clip.
- Download individual assets or a ZIP bundle.

## Important
Use only videos you own or have permission to download and edit. YouTube access, captions, age/region restrictions, private videos, and platform terms can prevent retrieval. This prototype does not bypass DRM, private access, or restrictions. A YouTube URL alone cannot guarantee access to the underlying video.

This is a **self-hosted app**, not a deployed public website yet. It needs a computer/server with FFmpeg and enough storage/CPU/RAM. Android is supported as the client/browser; video rendering happens on the server. A free phone browser cannot reliably run full video rendering and Whisper transcription in the background.

## Run locally / on a server
1. Install Python 3.10+ and FFmpeg.
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
4. Open `http://SERVER-IP:8000` on your phone.

For public internet access, deploy this folder to a server that permits FFmpeg and long-running jobs. Free hosting tiers often sleep, limit CPU, or restrict storage, so long videos may fail.

## Current prototype limitations
- Candidate scoring is a heuristic, not a promise of virality.
- Hook text is generated from transcript excerpts; review it before publishing.
- The app creates practical draft metadata, not guaranteed SEO performance.
- The first version processes one source job at a time.
