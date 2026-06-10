import os
import subprocess
import yt_dlp
import ffmpeg
import config

def get_cookies_for_url(url: str) -> str | None:
    """Return the correct cookie path based on the domain."""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return config.YT_COOKIES if os.path.exists(config.YT_COOKIES) else None
    elif "instagram.com" in url_lower:
        return config.IG_COOKIES if os.path.exists(config.IG_COOKIES) else None
    elif "tiktok.com" in url_lower:
        return config.TT_COOKIES if os.path.exists(config.TT_COOKIES) else None
    return None

def extract_formats(url: str) -> dict:
    """Extract format details and separate into sorted video and audio catalogs."""
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

    formats = info.get('formats', [])
    video_options = []
    audio_options = []

    for fmt in formats:
        # Check if it has a file size estimate
        size = fmt.get('filesize') or fmt.get('filesize_approx') or 0
        size_mb = round(size / (1024 * 1024), 1) if size > 0 else "Unknown Size"
        
        # Audio extraction filter
        if fmt.get('vcodec') == 'none' and fmt.get('acodec') != 'none':
            ext = fmt.get('ext', 'm4a')
            abr = fmt.get('abr') or 0  # Audio bitrate
            audio_options.append({
                'format_id': fmt['format_id'],
                'quality': f"{int(abr)}kbps {ext}",
                'size_mb': size_mb,
                'bitrate': abr
            })
            
        # Video extraction filter (We prioritize merged/standard videos containing audio)
        elif fmt.get('vcodec') != 'none':
            resolution = fmt.get('height')
            if resolution:
                video_options.append({
                    'format_id': fmt['format_id'],
                    'quality': f"{resolution}p",
                    'size_mb': size_mb,
                    'height': resolution
                })

    # Sort video by resolution (descending) and audio by bitrate (descending)
    video_options = sorted(video_options, key=lambda x: x['height'], reverse=True)
    audio_options = sorted(audio_options, key=lambda x: x['bitrate'], reverse=True)

    # De-duplicate entries (keep only the best file size for duplicate resolution/bitrates)
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
        'duration': info.get('duration', 0),
        'thumbnail': info.get('thumbnail'),
        'videos': unique_videos[:5], # Limit to top 5 qualities for clean button grids
        'audios': unique_audios[:5]
    }

def convert_thumbnail_to_jpeg(input_path: str, cache_id: str) -> str:
    """Uses FFmpeg to crop and pad the thumbnail into a standard 320x320 black-padded square JPEG."""
    output_path = f"cache/{cache_id}_thumb.jpg"
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
        # Fallback to copy/rename if ffmpeg scaling fails
        return input_path

def probe_video_dimensions(file_path: str) -> tuple[int, int, int]:
    """Return (width, height, duration) of the media file using ffmpeg probe."""
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

def download_media(url: str, format_id: str, format_type: str, cache_id: str) -> dict:
    """Download the file using cookies, postprocess it, and extract thumbnails."""
    os.makedirs("cache", exist_ok=True)
    out_tmpl = f"cache/{cache_id}_%(title)s.%(ext)s"
    cookie_path = get_cookies_for_url(url)
    
    ydl_opts = {
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
    }
    if cookie_path:
        ydl_opts['cookiefile'] = cookie_path
        
    if format_type == 'v':
        # YouTube splits high-quality streams. We must merge the chosen video stream with bestaudio
        ydl_opts['format'] = f"{format_id}+bestaudio/best"
        ydl_opts['merge_output_format'] = 'mp4'
    else:
        # Convert audio to MP3
        ydl_opts['format'] = format_id
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }]

    ydl_opts['writethumbnail'] = True
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        # Correct local paths post conversion
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

        # Search for saved thumbnails
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
