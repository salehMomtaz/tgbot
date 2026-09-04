"""
On-demand binary and video splitting generators.

Mirrors the original utils/downloader.py split functions exactly.
"""

import os
import subprocess
import json


def split_file_generator(file_path: str, max_chunk_size_bytes: int, hard_limit_bytes: int | None = None):
    """
    On-Demand sequential splitter:
    Yields paths of split binary parts one-by-one.
    Caps extra disk space to just ONE part (max 2GB or 4GB) instead of duplicating storage.
    If hard_limit_bytes is provided, chunks are clamped to never exceed it (safety margin).
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    if hard_limit_bytes is not None and hard_limit_bytes > 0:
        max_chunk_size_bytes = min(max_chunk_size_bytes, hard_limit_bytes)

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


def split_video_by_size_generator(file_path: str, target_size_bytes: int, hard_limit_bytes: int):
    """
    On-Demand video splitter using ffmpeg (-c copy, keyframe cuts).
    Yields paths of independently playable segments one-by-one.
    Estimates segment duration from target size, then verifies each output
    against the hard limit and re-cuts with shorter duration if exceeded.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size <= target_size_bytes:
        yield file_path
        return

    # Probe total duration securely
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", file_path],
            capture_output=True, text=True
        )
        probe_data = json.loads(probe.stdout)
        total_duration = float(probe_data.get("format", {}).get("duration", 0.0))
    except Exception:
        total_duration = 0.0

    if total_duration <= 0.0:
        # Fallback: if we cannot probe duration, split is impossible. Yield as single part.
        yield file_path
        return

    # Average bitrate (bytes/sec) -> seconds per target chunk
    bytes_per_sec = file_size / total_duration
    base_seg_seconds = max(1.0, target_size_bytes / bytes_per_sec)

    dir_name = os.path.dirname(file_path)
    basename = os.path.basename(file_path)
    root, ext = os.path.splitext(basename)
    if not ext:
        ext = ".mp4"

    start = 0.0
    part_num = 1
    seg_seconds = base_seg_seconds

    while start < total_duration - 0.1:
        part_path = os.path.join(dir_name, f"{root}.part{part_num:03d}{ext}")
        attempt_seconds = seg_seconds

        try:
            for _ in range(5):  # retry loop to respect hard limit
                cmd = [
                    "ffmpeg", "-y", "-ss", f"{start:.3f}",
                    "-i", file_path, "-t", f"{attempt_seconds:.3f}",
                    "-c", "copy", "-avoid_negative_ts", "make_zero",
                    part_path
                ]
                subprocess.run(cmd, capture_output=True, check=True)

                if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
                    raise RuntimeError("ffmpeg produced empty segment")

                if os.path.getsize(part_path) <= hard_limit_bytes:
                    break

                # Too big (keyframe spacing); shrink and retry
                os.remove(part_path)
                attempt_seconds *= 0.75
            else:
                # Could not get under hard limit after retries
                raise RuntimeError(
                    f"Segment exceeds hard limit even after retries: {part_path}"
                )

            # Capture the size BEFORE yielding: the consumer (uploader)
            # deletes each part right after sending it, then resumes this
            # generator — statting after resume measured a deleted file and
            # always fell back to target_size_bytes, so adaptation never
            # adapted. A local variable survives the yield; the file may not.
            actual = os.path.getsize(part_path) if os.path.exists(part_path) else target_size_bytes

            yield part_path

            start += attempt_seconds
            part_num += 1
            # Adapt next estimate from the actual yielded size
            if actual > 0:
                seg_seconds = max(1.0, attempt_seconds * (target_size_bytes / actual))

        except Exception as e:
            if os.path.exists(part_path):
                os.remove(part_path)
            raise e