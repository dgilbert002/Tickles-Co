"""
Media Analyzer Service
======================
Processes downloaded media files from Telegram (and other sources):
- Photos → OpenAI Vision API for chart/content analysis
- Voice notes → Speech-to-text transcription
- Videos → Audio extraction → transcription
- Results stored back in news_items.ai_analysis and detail fields

The analyzer runs as a background queue processor, picking up items
that have media files and haven't been analyzed yet.
"""

import os
import re
import json
import base64
import logging
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional, Dict, List, Any
from collections import deque

logger = logging.getLogger("jarvais.media_analyzer")


def _check_ffmpeg() -> bool:
    """Check if ffmpeg and ffprobe are available on PATH. Cached after first call."""
    if hasattr(_check_ffmpeg, "_result"):
        return _check_ffmpeg._result
    import shutil
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    ok = ff is not None and fp is not None
    if not ok:
        missing = []
        if not ff:
            missing.append("ffmpeg")
        if not fp:
            missing.append("ffprobe")
        logger.error(
            f"[MediaAnalyzer] {' and '.join(missing)} not found on PATH. "
            "Video/audio processing will be skipped. Install ffmpeg: "
            "https://ffmpeg.org/download.html or run 'winget install ffmpeg'")
    _check_ffmpeg._result = ok
    return ok


# Supported media types and their processing methods
PHOTO_TYPES = {"photo"}
AUDIO_TYPES = {"voice", "audio"}
VIDEO_TYPES = {"video", "video_note"}

# Media directory
MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "telegram_media")


class MediaAnalyzer:
    """
    Analyzes media files attached to news items.
    
    - Photos: Sent to OpenAI Vision API with context about the source
    - Voice/Audio: Transcribed using manus-speech-to-text or OpenAI Whisper
    - Video: Audio extracted via ffmpeg, then transcribed
    
    Results are stored in the news_item's ai_analysis field and appended
    to the detail field for inclusion in AI summaries.
    """

    _SOUL_CACHE: dict = {}

    def __init__(self):
        self._queue: deque = deque()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stats = {
            "photos_analyzed": 0,
            "voice_transcribed": 0,
            "videos_transcribed": 0,
            "errors": 0,
            "total_cost_usd": 0.0,
        }
        self._lock = threading.Lock()

    def _load_agent_soul(self, agent_id: str, fallback: str) -> str:
        """Load an agent's soul from DB for use as system_prompt."""
        if agent_id in self._SOUL_CACHE:
            return self._SOUL_CACHE[agent_id]
        try:
            from core.config_loader import get_agent_soul
            from db.database import get_db
            soul_data = get_agent_soul(get_db(), agent_id)
            if soul_data:
                soul = soul_data.get("soul") or soul_data.get("identity_prompt") or fallback
                if len(soul) > 20:
                    self._SOUL_CACHE[agent_id] = soul
                    return soul
        except Exception:
            pass
        self._SOUL_CACHE[agent_id] = fallback
        return fallback

    def start(self):
        """Start the background media analysis thread."""
        if self._running:
            return
        self._running = True
        self._recover_queued_jobs()
        self._thread = threading.Thread(target=self._process_loop, daemon=True, name="media-analyzer")
        self._thread.start()
        logger.info("Media Analyzer started")

    def _recover_queued_jobs(self):
        """Recover orphaned media_jobs from DB that were queued but never processed
        (e.g., after app restart). Re-queues them to the in-memory deque."""
        try:
            from db.database import get_db
            db = get_db()
            rows = db.fetch_all(
                "SELECT id, news_item_id, source, source_detail, author, media_type, media_url "
                "FROM media_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 50"
            )
            recovered = 0
            _skip_domains = ("discord.com/channels/",)
            for row in (rows or []):
                url = row.get("media_url") or ""
                if row.get("media_type") == "video" and url:
                    if any(d in url for d in _skip_domains):
                        try:
                            db.execute(
                                "UPDATE media_jobs SET status = 'failed', "
                                "error = 'Unsupported URL (Discord channel link)' "
                                "WHERE id = %s AND status = 'queued'",
                                (row["id"],))
                        except Exception:
                            pass
                        continue
                    ctx = {
                        "source": row.get("source", "unknown"),
                        "source_detail": row.get("source_detail", ""),
                        "author": row.get("author", ""),
                    }
                    # Fetch headline for passcode extraction (Zoom recordings)
                    nid = row.get("news_item_id")
                    if nid:
                        ni = db.fetch_one(
                            "SELECT headline, detail FROM news_items WHERE id = %s", (nid,))
                        if ni:
                            ctx["headline"] = ((ni.get("headline") or "") + "\n"
                                               + (ni.get("detail") or ""))[:500]
                            ctx["news_item_id"] = nid
                    self._queue.append({
                        "type": "video_url",
                        "job_id": row["id"],
                        "video_url": row["media_url"],
                        "news_item_id": nid,
                        "context": ctx,
                        "queued_at": datetime.utcnow().isoformat(),
                    })
                    recovered += 1
            if recovered:
                logger.info(f"Media Analyzer: recovered {recovered} queued jobs from DB")
        except Exception as e:
            logger.warning(f"Media Analyzer: failed to recover queued jobs: {e}")

    def stop(self):
        """Stop the background thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Media Analyzer stopped")

    def queue_item(self, news_item_id: int, media_list: List[Dict], context: Dict):
        """
        Queue a news item for media analysis.
        
        Args:
            news_item_id: The database ID of the news_item
            media_list: List of {"type": "photo|voice|video", "path": "/path/to/file"}
            context: {"source": "telegram", "source_detail": "...", "author": "...", "headline": "..."}
        """
        if not media_list:
            return
        
        # Only queue items that have actual downloaded files
        valid_media = [m for m in media_list if m.get("path") and os.path.exists(m["path"])]
        if not valid_media:
            return
        
        with self._lock:
            if any(q["news_item_id"] == news_item_id for q in self._queue):
                logger.debug(f"[MediaAnalyzer] news_item {news_item_id} already in queue, skipping")
                return
            self._queue.append({
                "news_item_id": news_item_id,
                "media": valid_media,
                "context": context,
                "queued_at": datetime.utcnow().isoformat(),
            })
        logger.debug(f"Queued {len(valid_media)} media files for news_item {news_item_id}")

    def get_stats(self) -> Dict:
        """Return current stats."""
        return dict(self._stats)

    def _process_loop(self):
        """Background loop that processes queued media items."""
        last_cleanup = 0
        cleanup_interval = 6 * 3600  # Run cleanup every 6 hours
        while self._running:
            try:
                item = None
                with self._lock:
                    if self._queue:
                        item = self._queue.popleft()

                if item:
                    self._process_item(item)
                else:
                    time.sleep(2)

                # Periodic cleanup of old video files (>7 days)
                now = time.time()
                if now - last_cleanup > cleanup_interval:
                    last_cleanup = now
                    self._cleanup_old_videos()
            except Exception as e:
                logger.error(f"Media analyzer loop error: {e}")
                self._stats["errors"] += 1
                time.sleep(5)

    def _cleanup_old_videos(self, max_age_days: int = 7):
        """Delete downloaded video files older than max_age_days."""
        video_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "video_downloads")
        if not os.path.exists(video_dir):
            return
        cutoff = time.time() - (max_age_days * 86400)
        removed = 0
        for fname in os.listdir(video_dir):
            fpath = os.path.join(video_dir, fname)
            try:
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info(f"Video cleanup: removed {removed} files older than {max_age_days} days")

    def _process_item(self, item: Dict):
        """Process a single queued item (media files or video URL download)."""
        # Handle video URL downloads (yt-dlp)
        if item.get("type") == "video_url":
            self._process_video_url(item)
            return

        news_item_id = item["news_item_id"]
        media_list = item["media"]
        context = item["context"]

        # Skip if media analysis already exists for this item (prevents re-analysis on crash recovery)
        try:
            from db.database import get_db
            existing = get_db().fetch_one(
                "SELECT ai_analysis FROM news_items WHERE id = %s", (news_item_id,)
            )
            if existing and existing.get("ai_analysis"):
                logger.debug(f"[MediaAnalyzer] Skipping news_item {news_item_id}: already has ai_analysis")
                self._stats["skipped"] = self._stats.get("skipped", 0) + 1
                return
        except Exception:
            pass

        all_analyses = []
        all_transcriptions = []
        
        for media in media_list:
            media_type = media.get("type", "")
            media_path = media.get("path", "")
            
            if not os.path.exists(media_path):
                logger.debug(f"Media file not found: {media_path}")
                continue
            
            try:
                if media_type in PHOTO_TYPES:
                    analysis = self._analyze_photo(media_path, context)
                    if analysis:
                        all_analyses.append(f"[PHOTO ANALYSIS]\n{analysis}")
                        self._stats["photos_analyzed"] += 1
                
                elif media_type in AUDIO_TYPES:
                    transcript = self._transcribe_audio(media_path, context)
                    if transcript:
                        all_transcriptions.append(f"[VOICE TRANSCRIPT]\n{transcript}")
                        self._stats["voice_transcribed"] += 1
                        # Feed transcript to source AI model for analysis
                        voice_analysis = self._analyze_transcript(transcript, context)
                        if voice_analysis:
                            all_analyses.append(f"[VOICE ANALYSIS]\n{voice_analysis}")
                
                elif media_type in VIDEO_TYPES:
                    video_result = self._transcribe_video(media_path, context)
                    if video_result:
                        # Video results may contain both transcripts and frame analyses
                        all_analyses.append(f"[VIDEO ANALYSIS]\n{video_result}")
                        self._stats["videos_transcribed"] += 1
                
            except Exception as e:
                logger.error(f"Error processing {media_type} at {media_path}: {e}")
                self._stats["errors"] += 1
        
        # Store results back in the database
        if all_analyses or all_transcriptions:
            self._store_results(news_item_id, all_analyses, all_transcriptions)

    def _process_video_url(self, item: Dict):
        """Process a video URL item: download via yt-dlp, then analyze."""
        job_id = item.get("job_id")
        video_url = item.get("video_url", "")
        news_item_id = item.get("news_item_id")
        context = item.get("context", {})

        logger.info(f"[VideoURL] Processing: {video_url[:80]} (job={job_id})")

        # Skip if this job was already completed (crash recovery guard)
        if job_id:
            try:
                from db.database import get_db
                job_row = get_db().fetch_one(
                    "SELECT status, ai_analysis FROM media_jobs WHERE id = %s", (job_id,)
                )
                if job_row and job_row.get("status") == "complete" and job_row.get("ai_analysis"):
                    logger.debug(f"[VideoURL] Skipping job {job_id}: already complete")
                    return
            except Exception:
                pass

        # Step 1: Download (pass context for Zoom passcode support)
        local_path = self._download_video_url(video_url, job_id, context)
        if not local_path:
            return

        # Step 2: Analyze (transcribe audio + keyframes)
        try:
            if job_id:
                self._update_job_status(job_id, "analyzing")

            analysis = self._transcribe_video(local_path, context)

            if analysis:
                self._stats["videos_transcribed"] += 1

                # Store in media_jobs (allow larger output for long videos)
                if job_id:
                    self._update_job_field(job_id, "ai_analysis", analysis[:50000])
                    self._update_job_status(job_id, "complete")

                # Store in news_items if we have a news_item_id
                if news_item_id:
                    self._store_results(news_item_id, [f"[VIDEO ANALYSIS]\n{analysis}"], [])

                # Step 3: Feed into signal pipeline so trades/setups become parsed_signals
                self._extract_video_signals(analysis, news_item_id, context)

                logger.info(f"[VideoURL] Complete: {video_url[:60]} ({len(analysis)} chars)")
            else:
                if job_id:
                    self._update_job_status(job_id, "failed", error="No analysis produced")

        except Exception as e:
            logger.error(f"[VideoURL] Analysis failed: {e}")
            if job_id:
                self._update_job_status(job_id, "failed", error=str(e)[:500])
        finally:
            # Clean up downloaded file after analysis
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except Exception:
                pass

    def _analyze_photo(self, image_path: str, context: Dict) -> Optional[str]:
        """
        Analyze a photo using the source-assigned vision model (from source_model_assignments).
        Falls back to gpt-4.1-mini if no assignment exists.
        Uses model_interface for unified AI access and cost tracking.
        """
        try:
            from core.model_interface import get_model_interface
            mi = get_model_interface()

            # Read and encode the image
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")

            ext = os.path.splitext(image_path)[1].lower()
            mime_type = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
            }.get(ext, "image/jpeg")

            source_name = context.get("source", "telegram")
            author = context.get("author", "unknown")
            headline = context.get("headline", "")
            source_detail = context.get("source_detail", source_name)

            # Look up model from source_model_assignments
            model_id, provider = self._get_source_model(source_name, "image")

            _LENS_FALLBACK = (
                "You are Lens, the Image & Chart Analyst at JarvAIs. Analyze the attached image:\n"
                "1. If it's a TRADING CHART: Identify instrument, timeframe, key levels, patterns, "
                "indicators, and implied trade direction. Note entry/exit levels, stop loss, take profit.\n"
                "2. If it's a SCREENSHOT of text/news: Extract and summarize key information.\n"
                "3. If it's any other image: Describe what's relevant to financial markets.\n"
                "Be concise but thorough. Focus on actionable information."
            )
            system_prompt = self._load_agent_soul("lens", _LENS_FALLBACK)

            user_prompt_text = f"Source: {source_detail} | Author: {author}"
            if headline:
                user_prompt_text += f" | Context: {headline}"
            user_prompt_text += "\n\nAnalyze this image:"

            user_content = [
                {"type": "text", "text": user_prompt_text},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime_type};base64,{image_data}",
                    "detail": "high"
                }}
            ]

            result = mi.query_with_model(
                model_id=model_id,
                provider=provider,
                role="photo_analysis",
                system_prompt=system_prompt,
                user_prompt=user_content,
                max_tokens=500,
                temperature=0.2,
                context="media_photo_analysis",
                source=source_name,
                source_detail=source_detail,
                author=author,
                news_item_id=context.get("news_item_id"),
                media_type="image", duo_id=None,
            )

            if result.success and result.content:
                self._stats["total_cost_usd"] += result.cost_usd
                src = source_detail or source_name
                nid = context.get("news_item_id")
                origin = f"source={src} author={author}" + (f" news#{nid}" if nid else "")
                logger.info(f"Photo analyzed: {os.path.basename(image_path)} "
                            f"({len(result.content)} chars, model={model_id}, ${result.cost_usd:.4f}) "
                            f"[{origin}]")
                return result.content
            else:
                logger.warning(f"Photo analysis failed: {result.error_message}")
                return None

        except Exception as e:
            logger.error(f"Photo analysis failed for {image_path}: {e}")
            return None

    def _get_source_model(self, source: str, media_type: str) -> tuple:
        """Look up model from source_model_assignments. Returns (model_id, provider)."""
        try:
            from db.database import get_db
            db = get_db()
            row = db.fetch_one(
                "SELECT model_id, model_provider FROM source_model_assignments "
                "WHERE source = %s AND media_type = %s AND is_enabled = 1",
                (source, media_type)
            )
            if row and row.get("model_id"):
                return row["model_id"], row["model_provider"]
        except Exception:
            pass
        return "openai/gpt-4.1-mini", "openrouter"

    def _analyze_transcript(self, transcript: str, context: Dict) -> Optional[str]:
        """
        After Whisper transcription, feed the transcript text to the source's
        voice model (from source_model_assignments) for trading-focused analysis.
        """
        if not transcript or len(transcript) < 20:
            return None

        try:
            from core.model_interface import get_model_interface
            mi = get_model_interface()

            source_name = context.get("source", "telegram")
            author = context.get("author", "unknown")
            source_detail = context.get("source_detail", source_name)

            # Use the voice model for post-transcription analysis
            model_id, provider = self._get_source_model(source_name, "voice")

            _VOX_FALLBACK = (
                "You are Vox, the Audio Analyst at JarvAIs. A voice message from a trading "
                "channel has been transcribed. Analyze the transcript for:\n"
                "1. Trading signals (buy/sell, instrument, entry/SL/TP levels)\n"
                "2. Market outlook or bias mentioned\n"
                "3. Key levels or events discussed\n"
                "4. Any actionable trading intelligence\n"
                "Be concise. If no trading content, summarize the key points briefly."
            )
            system_prompt = self._load_agent_soul("vox", _VOX_FALLBACK)

            user_prompt = (
                f"Source: {source_detail} | Author: {author}\n\n"
                f"Voice message transcript:\n{transcript[:4000]}"
            )

            result = mi.query_with_model(
                model_id=model_id,
                provider=provider,
                role="voice_analysis",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=600,
                temperature=0.2,
                context="media_voice_analysis",
                source=source_name,
                source_detail=source_detail,
                author=author,
                news_item_id=context.get("news_item_id"),
                media_type="voice", duo_id=None,
            )

            if result.success and result.content:
                self._stats["total_cost_usd"] += result.cost_usd
                src = source_detail or source_name
                nid = context.get("news_item_id")
                origin = f"source={src} author={author}" + (f" news#{nid}" if nid else "")
                logger.info(f"[VoiceAI] Transcript analyzed ({len(result.content)} chars, "
                            f"model={model_id}, ${result.cost_usd:.4f}) [{origin}]")
                return result.content
            else:
                logger.warning(f"[VoiceAI] Analysis failed: {result.error_message}")
                return None

        except Exception as e:
            logger.error(f"[VoiceAI] Transcript analysis failed: {e}")
            return None

    # Formats supported by OpenAI Whisper API
    WHISPER_SUPPORTED = {".flac", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".oga", ".ogg", ".wav", ".webm"}
    # Formats that need conversion to mp3 first
    NEEDS_CONVERSION = {".opus", ".amr", ".aac", ".wma", ".3gp"}

    WHISPER_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB safety margin (API limit is 25MB)

    def _transcribe_audio(self, audio_path: str, context: Dict = None) -> Optional[str]:
        """
        Transcribe an audio file to text using OpenAI Whisper API.
        Uses verbose_json format to get timestamped segments: [MM:SS] "text..."
        Converts unsupported formats to mp3 first.
        For files >20MB, splits into chunks and stitches timestamps back together.
        """
        converted_path = None
        try:
            ext = os.path.splitext(audio_path)[1].lower()

            if ext not in self.WHISPER_SUPPORTED:
                if not _check_ffmpeg():
                    logger.warning(f"[Whisper] Cannot convert {ext} — ffmpeg not installed")
                    return None
                converted_path = audio_path + ".converted.mp3"
                logger.info(f"[Whisper] Converting {ext} to mp3: {os.path.basename(audio_path)}")
                proc = subprocess.run(
                    ["ffmpeg", "-i", audio_path, "-acodec", "libmp3lame",
                     "-q:a", "4", "-y", converted_path],
                    capture_output=True, text=True, timeout=600
                )
                if proc.returncode != 0 or not os.path.exists(converted_path) or os.path.getsize(converted_path) < 100:
                    logger.warning(f"[Whisper] Audio conversion failed for {audio_path}: {proc.stderr[:200]}")
                    return None
                audio_path = converted_path

            file_size = os.path.getsize(audio_path)
            if file_size > self.WHISPER_MAX_FILE_BYTES:
                return self._transcribe_audio_chunked(audio_path, context)

            return self._transcribe_audio_single(audio_path, context)

        except Exception as e:
            logger.error(f"[Whisper] Transcription failed for {audio_path}: {e}")
            return None
        finally:
            if converted_path:
                try:
                    os.remove(converted_path)
                except Exception:
                    pass

    def _transcribe_audio_single(self, audio_path: str, context: Dict = None) -> Optional[str]:
        """Transcribe a single audio file (must be <=25MB) via Whisper API."""
        from openai import OpenAI
        client = OpenAI()

        with open(audio_path, "rb") as audio_file:
            transcript_response = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )

        timestamped_lines = []
        if hasattr(transcript_response, "segments") and transcript_response.segments:
            for seg in transcript_response.segments:
                start_sec = seg.get("start", 0) if isinstance(seg, dict) else getattr(seg, "start", 0)
                seg_text = seg.get("text", "").strip() if isinstance(seg, dict) else getattr(seg, "text", "").strip()
                if seg_text:
                    mins = int(start_sec // 60)
                    secs = int(start_sec % 60)
                    timestamped_lines.append(f"[{mins:02d}:{secs:02d}] \"{seg_text}\"")
            full_text = "\n".join(timestamped_lines)
        elif hasattr(transcript_response, "text") and transcript_response.text:
            full_text = transcript_response.text.strip()
        else:
            full_text = str(transcript_response).strip()

        if full_text:
            duration_sec = self._get_audio_duration(audio_path)
            cost = (duration_sec / 60.0) * 0.006
            self._stats["total_cost_usd"] += cost
            ctx = context or {}
            self._log_api_cost_granular(
                model="whisper-1", provider="openai",
                role="voice_transcription", context_tag="whisper_transcription",
                tokens_in=0, tokens_out=0, cost=cost,
                source=ctx.get("source"), source_detail=ctx.get("source_detail"),
                author=ctx.get("author"), news_item_id=ctx.get("news_item_id"),
                media_type="voice",
            )
            src = ctx.get("source_detail") or ctx.get("source", "?")
            author = ctx.get("author", "?")
            nid = ctx.get("news_item_id")
            origin = f"source={src} author={author}" + (f" news#{nid}" if nid else "")
            logger.info(f"[Whisper] Transcribed: {os.path.basename(audio_path)} "
                        f"({len(full_text)} chars, {len(timestamped_lines)} segments, "
                        f"{duration_sec:.0f}s, ${cost:.4f}) [{origin}]")
            return full_text
        logger.warning(f"[Whisper] Empty transcript for {audio_path}")
        return None

    def _transcribe_audio_chunked(self, audio_path: str, context: Dict = None) -> Optional[str]:
        """
        Split large audio (>20MB) into time-based chunks, transcribe each via Whisper,
        and stitch the timestamped segments back together with correct offsets.
        """
        duration_sec = self._get_audio_duration(audio_path)
        file_size = os.path.getsize(audio_path)

        bytes_per_sec = file_size / max(duration_sec, 1)
        chunk_duration = int(self.WHISPER_MAX_FILE_BYTES / bytes_per_sec * 0.9)
        chunk_duration = max(120, min(chunk_duration, 1200))  # 2min - 20min per chunk

        num_chunks = int(duration_sec / chunk_duration) + 1
        logger.info(f"[Whisper] Large file ({file_size / 1024 / 1024:.1f}MB, {duration_sec:.0f}s) "
                    f"-> splitting into {num_chunks} chunks of ~{chunk_duration}s each")

        all_lines = []
        total_cost = 0.0
        chunk_paths = []

        try:
            for i in range(num_chunks):
                start = i * chunk_duration
                if start >= duration_sec:
                    break

                chunk_path = f"{audio_path}.chunk_{i:03d}.mp3"
                chunk_paths.append(chunk_path)

                proc = subprocess.run(
                    ["ffmpeg", "-ss", str(start), "-t", str(chunk_duration),
                     "-i", audio_path, "-acodec", "libmp3lame", "-q:a", "4",
                     "-y", chunk_path],
                    capture_output=True, text=True, timeout=120
                )
                if proc.returncode != 0 or not os.path.exists(chunk_path) or os.path.getsize(chunk_path) < 100:
                    logger.warning(f"[Whisper] Chunk {i} extraction failed, skipping")
                    continue

                from openai import OpenAI
                client = OpenAI()
                with open(chunk_path, "rb") as cf:
                    resp = client.audio.transcriptions.create(
                        model="whisper-1", file=cf,
                        response_format="verbose_json",
                        timestamp_granularities=["segment"],
                    )

                if hasattr(resp, "segments") and resp.segments:
                    for seg in resp.segments:
                        seg_start = seg.get("start", 0) if isinstance(seg, dict) else getattr(seg, "start", 0)
                        seg_text = seg.get("text", "").strip() if isinstance(seg, dict) else getattr(seg, "text", "").strip()
                        if seg_text:
                            abs_sec = start + seg_start
                            mins = int(abs_sec // 60)
                            secs = int(abs_sec % 60)
                            all_lines.append(f"[{mins:02d}:{secs:02d}] \"{seg_text}\"")
                elif hasattr(resp, "text") and resp.text:
                    mins = int(start // 60)
                    secs = int(start % 60)
                    all_lines.append(f"[{mins:02d}:{secs:02d}] \"{resp.text.strip()}\"")

                chunk_cost = (min(chunk_duration, duration_sec - start) / 60.0) * 0.006
                total_cost += chunk_cost
                logger.info(f"[Whisper] Chunk {i+1}/{num_chunks}: {len(all_lines)} total segments so far")

        finally:
            for cp in chunk_paths:
                try:
                    os.remove(cp)
                except Exception:
                    pass

        if all_lines:
            self._stats["total_cost_usd"] += total_cost
            ctx = context or {}
            self._log_api_cost_granular(
                model="whisper-1", provider="openai",
                role="voice_transcription", context_tag="whisper_transcription_chunked",
                tokens_in=0, tokens_out=0, cost=total_cost,
                source=ctx.get("source"), source_detail=ctx.get("source_detail"),
                author=ctx.get("author"), news_item_id=ctx.get("news_item_id"),
                media_type="voice",
            )
            full_text = "\n".join(all_lines)
            src = ctx.get("source_detail") or ctx.get("source", "?")
            author = ctx.get("author", "?")
            nid = ctx.get("news_item_id")
            origin = f"source={src} author={author}" + (f" news#{nid}" if nid else "")
            logger.info(f"[Whisper] Chunked transcription complete: {len(all_lines)} segments, "
                        f"{duration_sec:.0f}s, ${total_cost:.4f} [{origin}]")
            return full_text
        logger.warning(f"[Whisper] Chunked transcription produced no output for {audio_path}")
        return None

    def _get_audio_duration(self, audio_path: str) -> float:
        """Get audio duration in seconds via ffprobe."""
        if not _check_ffmpeg():
            return 300.0
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                capture_output=True, text=True, timeout=10
            )
            return float(result.stdout.strip())
        except Exception:
            return 30.0  # Default assumption: 30 seconds

    def _transcribe_video(self, video_path: str, context: Dict = None) -> Optional[str]:
        """
        Process a video file intelligently:
        1. Probe the video to check for audio streams and duration
        2. If audio exists → extract and transcribe via Whisper
        3. If no audio (animated sticker/GIF) → extract keyframes and analyze visually
        4. If long video with audio → transcribe audio AND extract keyframes for visual analysis
        5. After all extraction, feed combined results to the source's video model for analysis
        """
        try:
            has_audio, duration, width, height = self._probe_video(video_path)
            basename = os.path.basename(video_path)
            logger.info(f"[Video] Probe: {basename} | duration={duration:.1f}s | "
                        f"{width}x{height} | audio={'yes' if has_audio else 'no'}")

            results = []

            # ── Audio extraction + Whisper transcription ──
            if has_audio:
                audio_path = video_path + ".audio.mp3"
                try:
                    timeout = max(300, int(duration / 2) + 60)  # Scale with duration, min 5min
                    proc = subprocess.run(
                        ["ffmpeg", "-i", video_path, "-vn", "-acodec", "libmp3lame",
                         "-q:a", "4", "-y", audio_path],
                        capture_output=True, text=True, timeout=timeout
                    )
                    if proc.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 100:
                        transcript = self._transcribe_audio(audio_path, context)
                        if transcript:
                            results.append(f"[AUDIO TRANSCRIPT]\n{transcript}")
                            self._stats["voice_transcribed"] += 1
                    else:
                        logger.warning(f"[Video] Audio extraction empty for {basename}")
                finally:
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass

            # ── Keyframe extraction for visual analysis ──
            # Scale frame count and interval based on video length
            if duration <= 10:
                keyframes = self._extract_keyframes(video_path, duration, max_frames=1)
            elif not has_audio:
                keyframes = self._extract_keyframes(video_path, duration, interval=3, max_frames=10)
            elif duration > 3600:  # >1 hour (Zoom sessions, long streams)
                keyframes = self._extract_keyframes(video_path, duration, interval=120, max_frames=30)
            elif duration > 600:   # >10 minutes
                keyframes = self._extract_keyframes(video_path, duration, interval=60, max_frames=20)
            elif duration > 30:
                keyframes = self._extract_keyframes(video_path, duration, interval=10, max_frames=5)
            else:
                keyframes = []

            if keyframes:
                for i, (kf_path, kf_timestamp) in enumerate(keyframes):
                    try:
                        mins = int(kf_timestamp // 60)
                        secs = int(kf_timestamp % 60)
                        ts_label = f"{mins:02d}:{secs:02d}"
                        analysis = self._analyze_photo(kf_path, context or {})
                        if analysis:
                            results.append(f"[FRAME at {ts_label}]\n{analysis}")
                    except Exception as e:
                        logger.warning(f"[Video] Keyframe analysis failed: {e}")
                    finally:
                        try:
                            os.remove(kf_path)
                        except Exception:
                            pass

            if results:
                combined = "\n\n".join(results)
                logger.info(f"[Video] Processed: {basename} ({len(results)} results, {duration:.0f}s)")

                # Feed combined results to the source's video model for a final synthesis
                video_synthesis = self._synthesize_video_analysis(combined, context)
                if video_synthesis:
                    return video_synthesis
                return combined
            else:
                logger.warning(f"[Video] No results from processing: {basename}")
                return None

        except Exception as e:
            logger.error(f"[Video] Processing failed for {video_path}: {e}")
            return None

    _SYNTH_FALLBACK = (
        "You are Reel, the Video Analyst at JarvAIs. A video from a trading channel has been "
        "processed: audio transcribed with timestamps and visual keyframes analyzed.\n\n"
        "TIMESTAMP CORRELATION RULES:\n"
        "- Transcript lines: [MM:SS] \"spoken text\"\n"
        "- Keyframe analyses: [FRAME at MM:SS] visual description\n"
        "- Use +/- 10 SECOND LENIENCY for correlating speech with visuals.\n"
        "- Group related speech + visuals together even if timestamps don't match exactly.\n\n"
        "SYNTHESIS REQUIREMENTS:\n"
        "1. Walk through chronologically, merging speech and visuals\n"
        "2. Extract ALL trading signals or setups mentioned/shown\n"
        "3. Identify instruments, price levels, entries, SL, TP\n"
        "4. Note market outlook/bias and confidence\n"
        "5. Flag risk warnings or caveats\n"
        "Be concise and actionable. If a chart contradicts verbal claims, note the discrepancy."
    )

    @property
    def _SYNTH_SYSTEM_PROMPT(self):
        return self._load_agent_soul("reel", self._SYNTH_FALLBACK)

    SYNTHESIS_SHORT_THRESHOLD = 8000  # chars -- below this, single-pass synthesis

    def _synthesize_video_analysis(self, raw_analysis: str, context: Dict = None) -> Optional[str]:
        """
        After transcription + keyframe analysis, synthesize into a cohesive summary.
        For short videos (<8K chars) uses a single pass.
        For long videos (>=8K chars) uses multi-pass: summarise time blocks then combine.
        """
        if not raw_analysis or len(raw_analysis) < 50:
            return None

        if len(raw_analysis) < self.SYNTHESIS_SHORT_THRESHOLD:
            return self._synthesize_single_pass(raw_analysis, context)

        return self._synthesize_multi_pass(raw_analysis, context)

    def _synthesize_single_pass(self, raw_analysis: str, context: Dict = None) -> Optional[str]:
        """Single-pass synthesis for short videos."""
        try:
            from core.model_interface import get_model_interface
            mi = get_model_interface()
            ctx = context or {}
            source_name = ctx.get("source", "telegram")
            model_id, provider = self._get_source_model(source_name, "video")

            user_prompt = (
                f"Source: {ctx.get('source_detail', source_name)} | Author: {ctx.get('author', '?')}\n\n"
                f"Timestamped video analysis:\n{raw_analysis}"
            )
            result = mi.query_with_model(
                model_id=model_id, provider=provider,
                role="video_synthesis", system_prompt=self._SYNTH_SYSTEM_PROMPT,
                user_prompt=user_prompt, max_tokens=1500, temperature=0.2,
                context="media_video_synthesis",
                source=source_name, source_detail=ctx.get("source_detail"),
                author=ctx.get("author"), news_item_id=ctx.get("news_item_id"),
                media_type="video", duo_id=None,
            )
            if result.success and result.content:
                self._stats["total_cost_usd"] += result.cost_usd
                logger.info(f"[VideoAI] Synthesis: {len(result.content)} chars, ${result.cost_usd:.4f}")
                return f"{result.content}\n\n--- Raw Analysis ---\n{raw_analysis}"
            return None
        except Exception as e:
            logger.error(f"[VideoAI] Single-pass synthesis failed: {e}")
            return None

    def _synthesize_multi_pass(self, raw_analysis: str, context: Dict = None) -> Optional[str]:
        """
        Multi-pass synthesis for long videos (Zoom sessions, streams).
        Pass 1: Split into time-windowed blocks, summarise each for trading signals.
        Pass 2: Combine all block summaries into a final holistic analysis.
        """
        try:
            from core.model_interface import get_model_interface
            mi = get_model_interface()
            ctx = context or {}
            source_name = ctx.get("source", "telegram")
            model_id, provider = self._get_source_model(source_name, "video")

            # Split raw analysis into ~7K char blocks at line boundaries
            lines = raw_analysis.split("\n")
            blocks = []
            current_block = []
            current_len = 0
            for line in lines:
                if current_len + len(line) > 7000 and current_block:
                    blocks.append("\n".join(current_block))
                    current_block = []
                    current_len = 0
                current_block.append(line)
                current_len += len(line) + 1
            if current_block:
                blocks.append("\n".join(current_block))

            logger.info(f"[VideoAI] Multi-pass: {len(blocks)} blocks from {len(raw_analysis)} chars")

            # Pass 1: Summarise each block
            block_system = (
                "You are a trading intelligence analyst extracting signals from a segment of a long "
                "trading video session. Extract ALL specific trade setups, price levels, entries, "
                "stop losses, take profits, instruments, and market outlook mentioned in this segment. "
                "Include the timestamp range. Be thorough -- do not miss any trade idea or setup."
            )
            block_summaries = []
            total_cost = 0.0
            for i, block in enumerate(blocks):
                block_prompt = (
                    f"Source: {ctx.get('source_detail', source_name)} | "
                    f"Author: {ctx.get('author', '?')} | Block {i+1}/{len(blocks)}\n\n"
                    f"Segment:\n{block}"
                )
                result = mi.query_with_model(
                    model_id=model_id, provider=provider,
                    role="video_synthesis", system_prompt=block_system,
                    user_prompt=block_prompt, max_tokens=1000, temperature=0.2,
                    context="media_video_block_synthesis",
                    source=source_name, source_detail=ctx.get("source_detail"),
                    author=ctx.get("author"), news_item_id=ctx.get("news_item_id"),
                    media_type="video", duo_id=None,
                )
                if result.success and result.content:
                    total_cost += result.cost_usd
                    block_summaries.append(f"[BLOCK {i+1}]\n{result.content}")
                    logger.info(f"[VideoAI] Block {i+1}/{len(blocks)}: "
                                f"{len(result.content)} chars, ${result.cost_usd:.4f}")

            if not block_summaries:
                return None

            # Pass 2: Final synthesis combining all block summaries
            combined_blocks = "\n\n".join(block_summaries)
            final_system = (
                f"{self._SYNTH_SYSTEM_PROMPT}\n\n"
                "ADDITIONAL CONTEXT: This is a LONG video session that was analysed in blocks. "
                "Below are the summaries from each time block. Combine them into a single coherent "
                "analysis. De-duplicate any trades mentioned across blocks. Preserve ALL unique "
                "trade setups with their specific levels."
            )
            final_prompt = (
                f"Source: {ctx.get('source_detail', source_name)} | Author: {ctx.get('author', '?')}\n\n"
                f"Block summaries from {len(blocks)}-segment analysis:\n\n{combined_blocks}"
            )
            final_result = mi.query_with_model(
                model_id=model_id, provider=provider,
                role="video_synthesis", system_prompt=final_system,
                user_prompt=final_prompt, max_tokens=2500, temperature=0.2,
                context="media_video_final_synthesis",
                source=source_name, source_detail=ctx.get("source_detail"),
                author=ctx.get("author"), news_item_id=ctx.get("news_item_id"),
                media_type="video", duo_id=None,
            )

            if final_result.success and final_result.content:
                total_cost += final_result.cost_usd
                self._stats["total_cost_usd"] += total_cost
                logger.info(f"[VideoAI] Multi-pass complete: {len(final_result.content)} chars, "
                            f"${total_cost:.4f} total across {len(blocks)+1} AI calls")
                return (f"{final_result.content}\n\n"
                        f"--- Block Summaries ({len(blocks)} segments) ---\n{combined_blocks}")

            self._stats["total_cost_usd"] += total_cost
            return f"--- Block Summaries ({len(blocks)} segments) ---\n{combined_blocks}"

        except Exception as e:
            logger.error(f"[VideoAI] Multi-pass synthesis failed: {e}")
            return None

    def _extract_video_signals(self, analysis: str, news_item_id: int = None, context: Dict = None):
        """
        After video synthesis, re-queue the enriched news_item through the signal
        pipeline so any trade setups (symbol, direction, entry, SL, TP) are extracted
        as parsed_signals and flow into alpha assessment.
        """
        if not news_item_id or not analysis:
            return
        try:
            from db.database import get_db
            db = get_db()

            news_item = db.fetch_one("SELECT * FROM news_items WHERE id = %s", (news_item_id,))
            if not news_item:
                return

            # Check if already parsed to avoid duplicates
            existing = db.fetch_one(
                "SELECT id FROM parsed_signals WHERE news_item_id = %s LIMIT 1", (news_item_id,))
            if existing:
                logger.debug(f"[VideoSignal] news#{news_item_id} already has parsed_signals, skipping")
                return

            from services.signal_ai import SignalParser
            parser = SignalParser()
            result = parser.parse_news_item(news_item)
            if result:
                logger.info(f"[VideoSignal] Extracted signal from video analysis: "
                            f"news#{news_item_id} -> {result.symbol} {result.direction}")
            else:
                logger.debug(f"[VideoSignal] No actionable signal found in video analysis "
                             f"for news#{news_item_id}")
        except Exception as e:
            logger.error(f"[VideoSignal] Signal extraction failed for news#{news_item_id}: {e}")

    def _probe_video(self, video_path: str) -> tuple:
        """
        Probe a video file with ffprobe to get metadata.
        Returns: (has_audio: bool, duration: float, width: int, height: int)
        """
        has_audio = False
        duration = 0.0
        width = 0
        height = 0

        if not _check_ffmpeg():
            return has_audio, 5.0, width, height

        try:
            # Check for audio streams
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10
            )
            has_audio = "audio" in result.stdout.strip()

            # Get duration and dimensions
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,duration",
                 "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=10
            )
            lines = result.stdout.strip().split("\n")
            for line in lines:
                parts = line.strip().split(",")
                if len(parts) >= 3:
                    # stream line: width,height,duration
                    try:
                        width = int(parts[0])
                        height = int(parts[1])
                        if parts[2] and parts[2] != 'N/A':
                            duration = float(parts[2])
                    except (ValueError, IndexError):
                        pass
                elif len(parts) == 1 and not duration:
                    # format duration line
                    try:
                        d = float(parts[0])
                        if d > 0:
                            duration = d
                    except (ValueError, IndexError):
                        pass

            # Fallback: if duration still 0, try format-level duration
            if duration <= 0:
                result = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                    capture_output=True, text=True, timeout=10
                )
                try:
                    duration = float(result.stdout.strip())
                except (ValueError, TypeError):
                    duration = 5.0  # Default assumption

        except Exception as e:
            logger.warning(f"ffprobe failed for {video_path}: {e}")
            duration = 5.0  # Default

        return has_audio, duration, width, height

    def _extract_keyframes(self, video_path: str, duration: float,
                           interval: int = 3, max_frames: int = 10) -> List[tuple]:
        """
        Extract keyframes from a video at specified intervals.
        Returns list of (path, timestamp_sec) tuples so callers can label each frame
        with the time it was extracted from.
        """
        if not _check_ffmpeg():
            return []

        keyframe_results = []
        basename = os.path.splitext(os.path.basename(video_path))[0]
        out_dir = os.path.dirname(video_path)

        if duration <= 0:
            duration = 5.0

        if max_frames == 1:
            timestamps = [duration / 2]
        else:
            timestamps = []
            t = 0.5  # Start slightly in
            while t < duration and len(timestamps) < max_frames:
                timestamps.append(t)
                t += interval

        for i, ts in enumerate(timestamps):
            out_path = os.path.join(out_dir, f"{basename}_frame{i}.jpg")
            try:
                proc = subprocess.run(
                    ["ffmpeg", "-ss", str(ts), "-i", video_path,
                     "-vframes", "1", "-q:v", "2", "-y", out_path],
                    capture_output=True, text=True, timeout=15
                )
                if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 500:
                    keyframe_results.append((out_path, ts))
                else:
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Keyframe extraction failed at {ts}s: {e}")

        logger.info(f"Extracted {len(keyframe_results)} keyframes from {os.path.basename(video_path)}")
        return keyframe_results

    def _store_results(self, news_item_id: int, analyses: List[str], transcriptions: List[str]):
        """Store media analysis results back in the news_item."""
        try:
            from db.database import get_db
            db = get_db()
            
            # Combine all results
            all_results = analyses + transcriptions
            combined = "\n\n".join(all_results)
            
            if not combined:
                return
            
            # Update the news_item with media analysis
            # Append to detail field so it's included in AI summaries
            # Also store in ai_analysis field for the media-specific analysis
            db.execute("""
                UPDATE news_items 
                SET detail = CONCAT(COALESCE(detail, ''), %s),
                    ai_analysis = COALESCE(ai_analysis, %s)
                WHERE id = %s
            """, (
                f"\n\n--- MEDIA ANALYSIS ---\n{combined}",
                combined,
                news_item_id
            ))
            
            logger.info(f"Stored media analysis for news_item {news_item_id}: "
                        f"{len(analyses)} photo analyses, {len(transcriptions)} transcriptions")
            
        except Exception as e:
            logger.error(f"Failed to store media results for {news_item_id}: {e}")

    def _log_api_cost(self, role: str, tokens_in: int, tokens_out: int, cost: float):
        """Log API cost to the ai_api_log table (legacy, no granular fields)."""
        self._log_api_cost_granular(
            model="gpt-4.1-mini", provider="openai",
            role=role, context_tag="media_analysis",
            tokens_in=tokens_in, tokens_out=tokens_out, cost=cost,
        )

    def _log_api_cost_granular(self, model: str, provider: str, role: str,
                                context_tag: str, tokens_in: int, tokens_out: int,
                                cost: float, source: str = None,
                                source_detail: str = None, author: str = None,
                                news_item_id: int = None, media_type: str = None,
                                duo_id: str = None):
        """Log API cost to ai_api_log with full granular tracking fields."""
        try:
            from db.database import get_db
            get_db().log_api_call({
                "account_id": "default",
                "provider": provider,
                "model": model,
                "role": role,
                "context": context_tag,
                "token_count_input": tokens_in,
                "token_count_output": tokens_out,
                "cost_usd": cost,
                "latency_ms": 0,
                "success": True,
                "source": source,
                "source_detail": source_detail,
                "author": author,
                "news_item_id": news_item_id,
                "media_type": media_type,
                "duo_id": duo_id,
            })
        except Exception:
            pass

    # ── yt-dlp Video URL Download + Queue ──────────────────────────────
    # Regex patterns for detecting video URLs in message text
    VIDEO_URL_PATTERNS = [
        r'(https?://(?:www\.)?youtube\.com/watch\?[\w&=%-]+)',   # youtube.com/watch?v=xxx&si=yyy
        r'(https?://(?:www\.)?youtube\.com/live/[\w-]+)',         # youtube.com/live/xxx
        r'(https?://(?:www\.)?youtube\.com/shorts/[\w-]+)',       # youtube.com/shorts/xxx
        r'(https?://youtu\.be/[\w-]+)',                           # youtu.be/xxx
        r'(https?://(?:www\.)?twitter\.com/\w+/status/\d+)',      # twitter.com
        r'(https?://(?:www\.)?x\.com/\w+/status/\d+)',            # x.com
        r'(https?://(?:www\.)?rumble\.com/v[\w-]+[\w.-]*\.html)',  # rumble.com/vXXX.html
        r'(https?://[\w.]*zoom\.us/rec/share/[\w._-]+)',          # zoom.us recording share links
        # NOTE: discord.com/channels/ removed -- yt-dlp cannot download Discord channel links
    ]

    def queue_video_url(self, video_url: str, news_item_id: int = None, context: Dict = None):
        """
        Queue a video URL (YouTube, X/Twitter, Zoom, Rumble, Discord) for background download + analysis.
        Creates a media_jobs row and queues for background processing.
        Deduplicates by URL to avoid re-downloading the same video.
        For Zoom recordings, extracts passcode from surrounding text if present.
        """
        ctx = context or {}
        try:
            # Extract Zoom passcode from headline/message text if present
            if "zoom.us/rec/" in video_url and not ctx.get("video_passcode"):
                headline = ctx.get("headline", "")
                match = self._ZOOM_PASSCODE_RE.search(headline)
                if match:
                    ctx["video_passcode"] = match.group(1)
                    logger.info(f"[VideoQueue] Extracted Zoom passcode from message text")
            from db.database import get_db
            db = get_db()

            # Dedup: skip if this URL was already queued/processed recently
            existing = db.fetch_one(
                "SELECT id, status FROM media_jobs WHERE media_url = %s "
                "AND created_at >= NOW() - INTERVAL 7 DAY ORDER BY created_at DESC LIMIT 1",
                (video_url,)
            )
            if existing:
                logger.debug(f"[VideoQueue] Skipping duplicate URL: {video_url[:60]} "
                             f"(job #{existing['id']} status={existing['status']})")
                return

            db.execute(
                "INSERT INTO media_jobs (news_item_id, source, source_detail, author, "
                "media_type, media_url, status) VALUES (%s, %s, %s, %s, 'video', %s, 'queued')",
                (news_item_id, ctx.get("source", "unknown"), ctx.get("source_detail"),
                 ctx.get("author"), video_url)
            )
            job_row = db.fetch_one("SELECT LAST_INSERT_ID() as id")
            job_id = job_row["id"] if job_row else None

            logger.info(f"[VideoQueue] Queued video URL: {video_url[:80]} (job={job_id})")

            # Add to in-memory queue for background processing
            with self._lock:
                self._queue.append({
                    "type": "video_url",
                    "job_id": job_id,
                    "video_url": video_url,
                    "news_item_id": news_item_id,
                    "context": ctx,
                    "queued_at": datetime.utcnow().isoformat(),
                })

        except Exception as e:
            logger.error(f"[VideoQueue] Failed to queue {video_url}: {e}")

    # Regex to extract Zoom passcodes from surrounding message text
    _ZOOM_PASSCODE_RE = re.compile(
        r'(?:passcode|password|pwd)\s*[:=]\s*([^\s\n]{3,20})', re.IGNORECASE)

    def _download_video_url(self, video_url: str, job_id: int = None,
                            context: Dict = None) -> Optional[str]:
        """
        Download a video from URL using yt-dlp.
        For Zoom recordings, passes passcode from context if available.
        Returns the local file path, or None if download fails.
        """
        # Skip URLs that yt-dlp cannot handle
        if "discord.com/channels/" in video_url:
            logger.debug(f"[yt-dlp] Skipping unsupported Discord channel URL: {video_url}")
            if job_id:
                self._update_job_status(job_id, "failed",
                                        error="Unsupported URL (Discord channel link)")
            return None

        download_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "video_downloads")
        os.makedirs(download_dir, exist_ok=True)

        safe_name = re.sub(r'[^\w]', '_', video_url[-60:])[:50]
        out_template = os.path.join(download_dir, f"vid_{job_id or 'x'}_{safe_name}.%(ext)s")

        # Check if file already exists on disk from a prior download
        import glob as _glob
        existing = _glob.glob(out_template.replace(".%(ext)s", ".*"))
        if existing and os.path.getsize(existing[0]) > 0:
            logger.debug(f"[yt-dlp] Already on disk: {os.path.basename(existing[0])}")
            return existing[0]

        try:
            if job_id:
                self._update_job_status(job_id, "downloading")

            is_zoom = "zoom.us" in video_url

            # Zoom recordings have a single combined stream — the YouTube-style
            # format selector fails. Use "best" (single stream) as primary for
            # Zoom, with the split format as fallback for YouTube/others.
            if is_zoom:
                fmt = "best[height<=720]/best"
            else:
                fmt = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"

            cmd = [
                "yt-dlp",
                "--no-playlist",
                "-f", fmt,
                "--merge-output-format", "mp4",
                "-o", out_template,
                "--max-filesize", "1500M",
                "--socket-timeout", "60",
            ]

            # For Zoom recordings, pass passcode if extracted from the Discord message
            ctx = context or {}
            passcode = ctx.get("video_passcode", "")
            if is_zoom and passcode:
                cmd.extend(["--video-password", passcode])
                logger.info(f"[yt-dlp] Using passcode for Zoom recording")

            cmd.append(video_url)

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=1800
            )

            if result.returncode != 0:
                stderr_snip = result.stderr[:500]
                # Zoom "format not available" is expected for passcode-protected
                # or expired recordings — log at WARNING, not ERROR.
                if is_zoom and "Requested format is not available" in stderr_snip:
                    logger.warning(
                        f"[yt-dlp] Zoom recording unavailable (likely passcode-protected "
                        f"or expired): {video_url}"
                    )
                else:
                    logger.error(f"[yt-dlp] Download failed: {stderr_snip[:300]}")
                if job_id:
                    self._update_job_status(job_id, "failed", error=stderr_snip)
                return None

            # Find the downloaded file
            import glob as glob_mod
            pattern = out_template.replace(".%(ext)s", ".*")
            files = glob_mod.glob(pattern)
            if files:
                local_path = files[0]
                if job_id:
                    self._update_job_field(job_id, "local_path", local_path)
                logger.info(f"[yt-dlp] Downloaded: {os.path.basename(local_path)} "
                            f"({os.path.getsize(local_path) / 1024 / 1024:.1f} MB)")
                return local_path
            else:
                logger.warning(f"[yt-dlp] No output file found for {video_url}")
                if job_id:
                    self._update_job_status(job_id, "failed", error="No output file found")
                return None

        except subprocess.TimeoutExpired:
            logger.error(f"[yt-dlp] Timeout downloading {video_url}")
            if job_id:
                self._update_job_status(job_id, "failed", error="Download timed out (30 min)")
            return None
        except FileNotFoundError:
            logger.error("[yt-dlp] yt-dlp not installed. Install with: pip install yt-dlp")
            if job_id:
                self._update_job_status(job_id, "failed", error="yt-dlp not installed")
            return None
        except Exception as e:
            logger.error(f"[yt-dlp] Download error: {e}")
            if job_id:
                self._update_job_status(job_id, "failed", error=str(e)[:500])
            return None

    def _update_job_status(self, job_id: int, status: str, error: str = None):
        """Update a media_jobs row status."""
        try:
            from db.database import get_db
            db = get_db()
            if error:
                db.execute("UPDATE media_jobs SET status=%s, error_message=%s WHERE id=%s",
                           (status, error, job_id))
            elif status == "complete":
                db.execute("UPDATE media_jobs SET status='complete', completed_at=NOW() WHERE id=%s",
                           (job_id,))
            else:
                db.execute("UPDATE media_jobs SET status=%s WHERE id=%s", (status, job_id))
        except Exception:
            pass

    def _update_job_field(self, job_id: int, field: str, value):
        """Update a single field on a media_jobs row."""
        try:
            from db.database import get_db
            get_db().execute(f"UPDATE media_jobs SET {field}=%s WHERE id=%s", (value, job_id))
        except Exception:
            pass

    def detect_video_urls(self, text: str) -> List[str]:
        """Detect video URLs (YouTube, X/Twitter, Zoom recordings, Rumble) in message text."""
        import re
        urls = []
        for pattern in self.VIDEO_URL_PATTERNS:
            urls.extend(re.findall(pattern, text or ""))
        return list(set(urls))


# ── Singleton ──
_media_analyzer: Optional[MediaAnalyzer] = None

def get_media_analyzer() -> MediaAnalyzer:
    """Get or create the singleton MediaAnalyzer instance."""
    global _media_analyzer
    if _media_analyzer is None:
        _media_analyzer = MediaAnalyzer()
    return _media_analyzer
