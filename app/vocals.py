"""Vocal isolation: strip background music before transcription.

Uses the open UVR MDX-Net "Kim Vocal 2" model (ONNX, ~67 MB, downloaded once) on
onnxruntime, which faster-whisper already installs. Opt-in because it costs time:
roughly realtime/3-5x on a CPU, much faster with an NVIDIA GPU (CUDA) or Apple CoreML.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

from .config import DATA_DIR
from .media import _NO_WINDOW, ffmpeg_path

log = logging.getLogger("hotclip.vocals")

MODEL_URL = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/Kim_Vocal_2.onnx"
MODEL_PATH = DATA_DIR / "models" / "Kim_Vocal_2.onnx"
MODEL_BYTES = 66_759_214
RATE = 44_100
N_FFT, HOP, DIM_F, DIM_T = 7680, 1024, 3072, 256
COMPENSATE = 1.009
CHUNK = HOP * (DIM_T - 1)          # samples per model window (~5.9 s)
TRIM = N_FFT // 2
GEN = CHUNK - 2 * TRIM             # new samples produced per window

_session = None
_lock = threading.Lock()
_WINDOW = np.hanning(N_FFT + 1)[:-1].astype(np.float32)   # periodic Hann (matches torch)


def ensure_model() -> Path:
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size == MODEL_BYTES:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".part")
    log.info("downloading vocal isolation model (67 MB, one time)")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    if tmp.stat().st_size != MODEL_BYTES:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("vocal isolation model download was incomplete")
    tmp.replace(MODEL_PATH)
    return MODEL_PATH


def _get_session():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort

            wanted = ["CUDAExecutionProvider", "CoreMLExecutionProvider", "CPUExecutionProvider"]
            providers = [p for p in wanted if p in ort.get_available_providers()]
            t0 = time.time()
            _session = ort.InferenceSession(str(ensure_model()), providers=providers)
            log.info("vocal isolation model ready on %s in %.1fs", _session.get_providers()[0],
                     time.time() - t0)
        return _session


def _stft(x: np.ndarray) -> np.ndarray:
    """[channels, CHUNK] -> [channels, F, DIM_T] complex (torch.stft, center=True)."""
    pad = np.pad(x, ((0, 0), (N_FFT // 2, N_FFT // 2)), mode="reflect")
    idx = np.arange(DIM_T)[:, None] * HOP + np.arange(N_FFT)[None, :]
    frames = pad[:, idx] * _WINDOW                        # [c, T, N_FFT]
    return np.fft.rfft(frames, axis=-1).transpose(0, 2, 1)


def _istft(spec: np.ndarray) -> np.ndarray:
    """[channels, F, DIM_T] complex -> [channels, CHUNK] (torch.istft, center=True)."""
    frames = np.fft.irfft(spec.transpose(0, 2, 1), n=N_FFT, axis=-1) * _WINDOW
    length = N_FFT + HOP * (DIM_T - 1)
    out = np.zeros((spec.shape[0], length), np.float32)
    norm = np.zeros(length, np.float32)
    for t in range(DIM_T):
        out[:, t * HOP:t * HOP + N_FFT] += frames[:, t]
        norm[t * HOP:t * HOP + N_FFT] += _WINDOW ** 2
    out /= np.maximum(norm, 1e-8)
    return out[:, N_FFT // 2:N_FFT // 2 + CHUNK]


def isolate(mix: np.ndarray) -> np.ndarray:
    """Stereo float32 [2, n] at 44.1 kHz -> vocals [2, n]."""
    session = _get_session()
    n = mix.shape[1]
    pad = GEN - (n % GEN)
    padded = np.concatenate([np.zeros((2, TRIM), np.float32), mix,
                             np.zeros((2, pad + TRIM), np.float32)], axis=1)
    out = []
    for i in range(0, n + pad, GEN):
        spec = _stft(padded[:, i:i + CHUNK])[:, :DIM_F]               # [2, F, T]
        x = np.stack([spec.real, spec.imag], axis=1).reshape(1, 4, DIM_F, DIM_T)
        y = session.run(None, {"input": x.astype(np.float32)})[0].reshape(2, 2, DIM_F, DIM_T)
        full = np.zeros((2, N_FFT // 2 + 1, DIM_T), np.complex64)
        full[:, :DIM_F] = y[:, 0] + 1j * y[:, 1]
        out.append(_istft(full)[:, TRIM:-TRIM])
    return np.concatenate(out, axis=1)[:, :n] * COMPENSATE


def vocals_16k(media_file: Path, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Decode part of a file, remove music, return mono float32 at 16 kHz for Whisper."""
    t0 = time.time()
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(start, 0):.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(media_file), "-vn", "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    mix = np.frombuffer(raw, np.float32).reshape(-1, 2).T.copy()
    if mix.shape[1] < RATE // 2:
        return np.zeros(0, np.float32)
    voice = isolate(mix).mean(axis=0)
    # 44.1 kHz -> 16 kHz via ffmpeg's resampler (fast and high quality)
    res = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "f32le",
                          "-ar", str(RATE), "-ac", "1", "-i", "-", "-ar", "16000", "-f", "f32le", "-"],
                         input=voice.astype(np.float32).tobytes(), capture_output=True, check=True,
                         creationflags=_NO_WINDOW).stdout
    secs = mix.shape[1] / RATE
    took = time.time() - t0
    log.debug("isolated vocals for %.0fs of audio in %.1fs (%.1fx realtime)", secs, took, secs / max(took, 0.01))
    return np.frombuffer(res, np.float32).copy()


_TONAL_WIN = np.hanning(2048).astype(np.float32)


def tonality(x: np.ndarray) -> float:
    """How much of a signal is sustained pitched notes (0..1): the share of spectral peaks
    that hold steady for ~90 ms. Music ~0.35-0.6; speech leakage, crowd and street noise
    stay below ~0.2 even when loud."""
    lo, hi = int(150 * 2048 / RATE), int(6000 * 2048 / RATE)
    frames = np.lib.stride_tricks.sliding_window_view(x, 2048)[::512] * _TONAL_WIN
    if len(frames) < 12:
        return 0.0
    level = 10 * np.log10(np.abs(np.fft.rfft(frames, axis=1))[:, lo:hi] ** 2 + 1e-12)
    peak = ((level[:, 1:-1] > level[:, :-2]) & (level[:, 1:-1] > level[:, 2:])
            & (level[:, 1:-1] > np.median(level, axis=1, keepdims=True) + 8))
    peak = np.pad(peak, ((0, 0), (1, 1)))
    near = peak | np.roll(peak, 1, 1) | np.roll(peak, -1, 1)
    k = 8
    held = peak[:-k].copy()
    for j in range(1, k + 1):
        held &= near[j:len(peak) - k + j]
    return float(held.sum() / max(peak[:-k].sum(), 1))


def music_profile(media_file: Path, start: float = 0.0, duration: float | None = None) -> dict:
    """Per second: level of the full mix and of the non-vocal part (music / instruments /
    effects), and how tonal that non-vocal part is. Used to find background music."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(start, 0):.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(media_file), "-vn", "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    mix = np.frombuffer(raw, np.float32).reshape(-1, 2).T.copy()
    if mix.shape[1] < RATE:
        return {"seconds": 0, "total_db": [], "rest_db": [], "tonal": []}
    rest = (mix - isolate(mix)).mean(axis=0)
    full = mix.mean(axis=0)
    n = full.size // RATE

    def db(x):
        return 20 * np.log10(np.sqrt(np.mean(x.reshape(n, RATE) ** 2, axis=1)) + 1e-9)

    tonal = [tonality(rest[i * RATE:(i + 1) * RATE]) for i in range(n)]
    return {"seconds": n, "total_db": db(full[: n * RATE]).round(1).tolist(),
            "rest_db": db(rest[: n * RATE]).round(1).tolist(), "tonal": np.round(tonal, 3).tolist()}
