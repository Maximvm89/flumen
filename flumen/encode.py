"""Encode a folder of numbered image frames into a review H.264 MP4.

Generalises turntable._encode_mp4 (which knows only the pipeline's own
frame_####.png output) to whatever an external render manager (BRQ) wrote:
the sequence's prefix, number padding and start frame are detected from the
files themselves, so 'E:\\render' full of anything_1001.png just works.
"""

from __future__ import annotations

import os
import re
import subprocess

# What ffmpeg can turn into a review MP4 with correct colours out of the box.
# EXR is deliberately absent: linear EXRs encode washed out without a proper
# colour transform — render PNGs for review, or convert first.
_IMAGE_EXTS = ("png", "jpg", "jpeg", "tif", "tiff", "tga")

_FRAME_RE = re.compile(r"^(.*?)(\d+)\.(" + "|".join(_IMAGE_EXTS) + r")$",
                       re.IGNORECASE)


def find_sequence(frames_dir: str):
    """Detect the image sequence in a folder: {pattern, start, end, count,
    gaps} where pattern is an ffmpeg printf name ('frame_%04d.png'). When
    several sequences share the folder the longest one wins. None if no
    numbered images are found."""
    groups: dict[tuple, list[int]] = {}
    try:
        names = os.listdir(frames_dir)
    except OSError:
        return None
    for n in names:
        m = _FRAME_RE.match(n)
        if not m:
            continue
        prefix, num, ext = m.group(1), m.group(2), m.group(3)
        groups.setdefault((prefix, len(num), ext), []).append(int(num))
    if not groups:
        return None
    key = max(groups, key=lambda k: len(groups[k]))
    prefix, pad, ext = key
    nums = sorted(groups[key])
    return {"pattern": f"{prefix}%0{pad}d.{ext}",
            "start": nums[0], "end": nums[-1], "count": len(nums),
            # ffmpeg walks the numbering contiguously — a hole ends the video
            # early, so the caller warns about it instead of wondering.
            "gaps": (nums[-1] - nums[0] + 1) - len(nums)}


def _ffmpeg_cmd(ffmpeg: str, pattern_path: str, start: int, fps, out_mp4: str):
    """The encode command — same look as the pipeline's dailies (H.264,
    CRF 18, yuv420p; odd dimensions rounded down a pixel instead of failing)."""
    return [ffmpeg, "-y", "-framerate", str(fps),
            "-start_number", str(int(start)), "-i", pattern_path,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", out_mp4]


def encode_frames(frames_dir: str, out_mp4: str = "", fps=24) -> str:
    """Encode the folder's detected sequence into an MP4 (default:
    '<folder>/<foldername>.mp4'). Returns the MP4 path, or '' on failure."""
    from .turntable import _ffmpeg_exe
    frames_dir = os.path.abspath(os.path.expanduser(frames_dir))
    seq = find_sequence(frames_dir)
    if not seq:
        print(f"error: no numbered image sequence found in {frames_dir} "
              f"(looked for *_NNNN.{'/'.join(_IMAGE_EXTS)}).")
        return ""
    if seq["gaps"]:
        print(f"warning: sequence has {seq['gaps']} missing frame(s) between "
              f"{seq['start']} and {seq['end']} — the video will stop at the "
              f"first hole.")
    if not out_mp4:
        leaf = os.path.basename(frames_dir.rstrip("/\\")) or "frames"
        out_mp4 = os.path.join(frames_dir, f"{leaf}.mp4")
    print(f"encoding {seq['count']} frame(s) ({seq['pattern']} "
          f"{seq['start']}-{seq['end']}) @ {fps} fps …")
    cmd = _ffmpeg_cmd(_ffmpeg_exe(), os.path.join(frames_dir, seq["pattern"]),
                      seq["start"], fps, out_mp4)
    try:
        subprocess.run(cmd, check=True)
    except Exception as exc:  # noqa: BLE001
        print("error: ffmpeg encode failed:", exc)
        return ""
    if not os.path.isfile(out_mp4):
        return ""
    print(f"video -> {out_mp4}")
    return out_mp4
