# utils/downloader.py
import os
import subprocess
import yt_dlp
import ffmpeg
import config

def get_cookies_for_url(url: str) -> str | None:
    """Return the correct cookie path based on the domain, falling back to a global cookies.txt file."""
    url_lower = url.lower()
    cookie_path = None
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        cookie_path = config.YT_COOKIES
    elif "instagram.com" in url_lower:
        cookie_path = config.IG_COOKIES
    elif "tiktok.com" in url_lower:
        cookie_path = config.TT_COOKIES
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        cookie_path = config.X_COOKIES
    else:
        # Fallback global cookies file for all other 1,000+ yt-dlp supported sites
        cookie_path = "cookies.txt"
        
    # Only use the cookies if the file exists and is not empty (greater than 0 bytes)
    if cookie_path and os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 0:
        return cookie_path
    return None

def estimate_format_size(fmt: dict, duration_seconds: int) -> int:
    """Estimates the file size of a format in bytes using bitrate or resolution mappings."""
    size = fmt.get('filesize') or fmt.get('filesize_approx') or 0
    if size > 0:
        return size
        
    tbr = fmt.get('tbr') or fmt.get('vbr') or fmt.get('abr') or 0
    if tbr > 0 and duration_seconds > 0:
        return int((duration_seconds * (tbr * 1000)) / 8)
        
    height = fmt.get('height')
    if duration_seconds > 0:
        duration_minutes = duration_seconds / 60
        if height:
            if height >= 1080:
                mb_per_min = 15
            elif height >= 720:
                mb_per_min = 8
            elif height >= 480:
                mb_per_min = 4
            else:
                mb_per_min = 2
        else:
            mb_per_min = 1.5
            
        return int(duration_minutes * mb_per_min * 1024 * 1024)
        
    return 0

def format_size_short(size_bytes: int) -> str:
    """Format file size into short, compact strings to prevent glass button text cuts."""
    if size_bytes <= 0:
        return "??"
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{round(size_mb / 1024, 1)}G"
    return f"{int(size_mb)}M"

def extract_formats(url: str) -> dict:
    cookie_path = get_cookies_for_url(url)
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            raise RuntimeError(f"Extraction failed: {str(e)}")

    duration_seconds = info.get('duration', 0)
    formats = info.get('formats', [])
    video_options = []
    audio_options = []

    for fmt in formats:
        size = estimate_format_size(fmt, duration_seconds)
        size_str = format_size_short(size)
        
        if fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':
            ext = fmt.get('ext', 'm4a')
            abr = fmt.get('abr') or 0
            audio_options.append({
                'format_id': fmt['format_id'],
                'quality': f"{int(abr)}k",
                'size_str': size_str,
                'bytes': size,
                'bitrate': abr
            })
            
        elif fmt.get('vcodec') != 'none':
            resolution = fmt.get('height')
            if resolution:
                warn_flag = " ⚠️" if size > (2000 * 1024 * 1024) else ""
                video_options.append({
                    'format_id': fmt['format_id'],
                    'quality': f"{resolution}p",
                    'size_str': f"{size_str}{warn_flag}",
                    'bytes': size,
                    'height': resolution
                })

    video_options = sorted(video_options, key=lambda x: x['height'], reverse=True)
    audio_options = sorted(audio_options, key=lambda x: x['bitrate'], reverse=True)

    unique_videos = []
    seen_heights = set()
    for v in video_options:
        if v['height'] not in seen_heights:
            unique_videos.append(v)
            seen_heights.add(v['height'])
            
    unique_audios = []
    seen_bitrates = set()
    for a in audio_options:
        if a['quality'] not in seen_bitrates:
            unique_audios.append(a)
            seen_bitrates.add(a['quality'])

    return {
        'title': info.get('title', 'Unknown Title'),
        'duration': duration_seconds,
        'thumbnail': info.get('thumbnail'),
        'videos': unique_videos[:5],
        'audios': unique_audios[:5]
    }

def convert_thumbnail_to_jpeg(input_path: str, cache_id: str) -> str:
    """Uses FFmpeg to crop and pad the thumbnail into a standard 320x320 black-padded square JPEG inside the task folder."""
    output_path = f"cache/{cache_id}/thumb.jpg"
    try:
        cmd = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-vf', 'scale=w=320:h=320:force_original_aspect_ratio=decrease,pad=320:320:(ow-iw)/2:(oh-ih)/2:black',
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return output_path
    except Exception:
        return input_path

def probe_video_dimensions(file_path: str) -> tuple[int, int, int]:
    try:
        probe = ffmpeg.probe(file_path)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        duration = int(float(probe['format']['duration']))
        if video_stream:
            width = int(video_stream['width'])
            height = int(video_stream['height'])
            return width, height, duration
        return 320, 320, duration
    except Exception:
        return 320, 320, 0

def download_media(url: str, format_id: str, format_type: str, cache_id: str, progress_fn=None) -> dict:
    """Download the file using cookies, postprocess it, and extract thumbnails into a secure task folder."""
    task_dir = f"cache/{cache_id}"
    os.makedirs(task_dir, exist_ok=True)
    out_tmpl = f"{task_dir}/%(title)s.%(ext)s"
    cookie_path = get_cookies_for_url(url)
    
    ydl_opts = {
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
    }
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path
        
    if format_type == 'v':
        ydl_opts['format'] = f"{format_id}+bestaudio/best"
        ydl_opts['merge_output_format'] = 'mp4'
    else:
        ydl_opts['format'] = format_id
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }]

    ydl_opts['writethumbnail'] = True
    
    if progress_fn:
        def ytdl_hook(d):
            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                progress_fn(downloaded, total)
        ydl_opts['progress_hooks'] = [ytdl_hook]
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        if format_type == 'a':
            base, _ = os.path.splitext(filename)
            filename = f"{base}.mp3"
        elif format_type == 'v':
            base, _ = os.path.splitext(filename)
            if not os.path.exists(filename):
                if os.path.exists(f"{base}.mp4"):
                    filename = f"{base}.mp4"
                elif os.path.exists(f"{base}.mkv"):
                    filename = f"{base}.mkv"

        base_path, _ = os.path.splitext(filename)
        thumb_path = None
        for ext in ['.jpg', '.jpeg', '.png', '.webp']:
            test_path = f"{base_path}{ext}"
            if os.path.exists(test_path):
                thumb_path = test_path
                break
            
        clean_thumb = None
        if thumb_path:
            clean_thumb = convert_thumbnail_to_jpeg(thumb_path, cache_id)

        return {
            'file_path': filename,
            'thumb_path': clean_thumb,
            'title': info.get('title', 'Unknown Title'),
            'duration': info.get('duration', 0),
            'uploader': info.get('uploader', 'Unknown Artist')
        }

def split_file_generator(file_path: str, max_chunk_size_bytes: int):
    """
    On-Demand sequential splitter:
    Yields paths of split binary parts one-by-one.
    Caps extra disk space to just ONE part (max 2GB or 4GB) instead of duplicating storage.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
        
    file_size = os.path.getsize(file_path)
    
    if file_size <= max_chunk_size_bytes:
        yield file_path
        return
        
    num_chunks = (file_size + max_chunk_size_bytes - 1) // max_chunk_size_bytes
    dir_name = os.path.dirname(file_path)
    basename = os.path.basename(file_path)
    
    BUFFER_SIZE = min(1024 * 1024, max_chunk_size_bytes)
    
    with open(file_path, "rb") as f_in:
        for part_num in range(1, num_chunks + 1):
            part_path = os.path.join(dir_name, f"{basename}.{part_num:03d}")
            bytes_remaining = max_chunk_size_bytes
            
            try:
                with open(part_path, "wb") as f_out:
                    while bytes_remaining > 0:
                        to_read = min(BUFFER_SIZE, bytes_remaining)
                        chunk = f_in.read(to_read)
                        if not chunk:
                            break
                        f_out.write(chunk)
                        bytes_remaining -= len(chunk)
                        
                yield part_path
                
            except Exception as e:
                if os.path.exists(part_path):
                    os.remove(part_path)
                raise e