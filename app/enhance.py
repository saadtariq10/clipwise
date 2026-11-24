"""Video enhancement: adaptive, measured settings that run on local ffmpeg.

Chosen with VMAF/SSIM tests against original footage (gameplay + animation):
- Low-res/compressed sources being upscaled ("restore"): light denoise + Lanczos +
  unsharp 0.8 gave +5..+11 VMAF with no SSIM loss. Sharpening 1.1 or strong denoise
  started to over-process (SSIM dropped), so they're not used.
- Sources already at (or above) the output size ("refine"): the full chain over-processed
  them, so only a gentle unsharp 0.4 is applied (+3..+5 VMAF, minimal change).
- Audio is normalized to -14 LUFS (YouTube's loudness target).
"""
from __future__ import annotations

DENOISE = "hqdn3d=1.5:1.5:4:4"
SHARPEN_RESTORE = "unsharp=5:5:0.8:3:3:0"
SHARPEN_REFINE = "unsharp=5:5:0.4:3:3:0"
AUDIO = "highpass=f=60,loudnorm=I=-14:TP=-1.5:LRA=11"
UPSCALE_THRESHOLD = 1.2   # scale factor above which a source counts as "being upscaled"

X264_HQ = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
NVENC_HQ = ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0"]


def mode_for(scale_factor: float) -> str:
    return "restore" if scale_factor >= UPSCALE_THRESHOLD else "refine"


def wrap(vf_scaled: str, scale_factor: float) -> str:
    """Add enhancement around an existing scale/crop filter chain."""
    if mode_for(scale_factor) == "restore":
        return f"{DENOISE},{vf_scaled},{SHARPEN_RESTORE}"
    return f"{vf_scaled},{SHARPEN_REFINE}"


# Standalone tool: user-chosen target quality and strength.
# Measured on YouTube's 1080p upscaled to 2K vs the real 4K master (VMAF, higher = closer):
# plain upscale 49.6, CAS 0.5-0.8 51.6-52.8, denoise + unsharp 0.8 54.5, 1.0 55.5.
SHARPEN = {"restore": {"natural": 0.6, "balanced": 1.0, "strong": 1.3},
           "refine": {"natural": 0.5, "balanced": 0.8, "strong": 1.1},
           "downscale": {"natural": 0.2, "balanced": 0.35, "strong": 0.5}}
COLOR = {"natural": "vibrance=intensity=0.10,eq=contrast=1.02",
         "balanced": "vibrance=intensity=0.18,eq=contrast=1.04",
         "strong": "vibrance=intensity=0.28,eq=contrast=1.06"}


def label(height: int) -> str:
    return {2160: "4K", 1440: "2K"}.get(height, f"{height}p")


def x264_for(out_h: int) -> list[str]:
    if max(out_h, 0) >= 2000:   # 4K: keep CPU encode time reasonable, bitrate still high
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "17"]


def standalone_plan(width: int, height: int, target: int = 0, strength: str = "balanced",
                    color: bool = True) -> dict:
    """Whole-video enhancement. `target` = output size of the short side (1080/1440/2160);
    0 = auto (upscale small sources to 1080, never downscale)."""
    short = min(width, height) or 1080
    goal = target or max(short, 1080)
    factor = goal / short
    out_w = int(round(width * factor / 2)) * 2
    out_h = int(round(height * factor / 2)) * 2
    ratio = width / height if height else 0
    sizes = {1080: (1920, 1080), 1440: (2560, 1440), 2160: (3840, 2160)}
    if goal in sizes and abs(ratio - 16 / 9) < 0.01:      # exact standard sizes
        out_w, out_h = sizes[goal]
    elif goal in sizes and abs(ratio - 9 / 16) < 0.01:
        out_h, out_w = sizes[goal]
    if factor >= UPSCALE_THRESHOLD:
        mode = "restore"
    elif factor <= 1 / UPSCALE_THRESHOLD:
        mode = "downscale"
    else:
        mode = "refine"
    strength = strength if strength in COLOR else "balanced"
    amount = SHARPEN[mode][strength]
    chain = []
    if mode != "downscale":          # compression noise/blocking (a hi-res source is clean)
        chain.append(DENOISE)
    if (out_w, out_h) != (width, height):
        chain.append(f"scale={out_w}:{out_h}:flags=lanczos")
    chain.append(f"unsharp=5:5:{amount}:3:3:0")
    if color:
        chain.append(COLOR[strength])
    chain.append("format=yuv420p")
    return {"vf": ",".join(chain), "mode": mode, "factor": round(factor, 2),
            "out_w": out_w, "out_h": out_h}


def layout_factor(layout: str, src_w: int, src_h: int) -> float:
    """How much the clip layouts scale the source (decides restore vs refine)."""
    if not src_w or not src_h:
        return 1.0
    if layout == "vertical_crop":        # source scaled so its height fills 1920
        return 1920 / src_h if src_w / src_h >= 9 / 16 else 1080 / src_w
    if layout == "vertical_blur":        # foreground scaled to 1080 wide
        return 1080 / src_w
    return min(1920 / src_w, 1080 / src_h)   # original 16:9
