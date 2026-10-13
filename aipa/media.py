"""Convert ComfyUI IMAGE / VIDEO inputs into PNG files the CLIs can look at."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import numpy as np
from PIL import Image


def sample_indices(total: int, n: int) -> list[int]:
    """Evenly spaced, unique frame indices covering the whole clip."""
    if total <= 0 or n <= 0:
        return []
    if n >= total:
        return list(range(total))
    idx = np.linspace(0, total - 1, n)
    return sorted(set(int(round(i)) for i in idx))


def tensor_to_pil(frame: Any) -> Image.Image:
    """[H,W,C] float tensor/ndarray in 0..1 -> PIL RGB or RGBA image."""
    if hasattr(frame, "detach"):
        frame = frame.detach().cpu().float().numpy()
    arr = np.asarray(frame)
    if arr.ndim == 2:
        arr = arr[..., None]
    channels = arr.shape[-1]
    if channels == 1:
        arr = np.repeat(arr, 3, axis=-1)
    elif channels == 2:  # grey + alpha
        arr = np.concatenate([np.repeat(arr[..., :1], 3, axis=-1), arr[..., 1:2]], axis=-1)
    elif channels > 4:
        arr = arr[..., :3]
    arr = (np.clip(arr, 0.0, 1.0) * 255.0).round().astype(np.uint8)
    return Image.fromarray(np.ascontiguousarray(arr))  # Pillow infers RGB / RGBA from the channel count


def flatten_alpha(img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    """Composite RGBA over a flat background so cut-outs stay cut out."""
    if img.mode == "RGBA":
        bg = Image.new("RGBA", img.size, background + (255,))
        return Image.alpha_composite(bg, img).convert("RGB")
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def resize_max_side(img: Image.Image, max_side: int) -> Image.Image:
    if max_side <= 0:
        return img
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    scale = max_side / float(longest)
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return img.resize(new_size, Image.LANCZOS)


def _save(img: Image.Image, run_dir: Path, prefix: str, n: int, max_side: int) -> Path:
    img = flatten_alpha(resize_max_side(img, max_side))
    p = Path(run_dir) / f"{prefix}_{n:02d}.png"
    img.save(p, format="PNG", optimize=True)
    return p


def save_frames(frames: Any, run_dir: Path, *, max_frames: int, max_side: int, prefix: str) -> list[Path]:
    """frames: [B,H,W,C] tensor. Saves up to `max_frames` evenly sampled PNGs."""
    total = int(frames.shape[0])
    return [
        _save(tensor_to_pil(frames[idx]), run_dir, prefix, n, max_side)
        for n, idx in enumerate(sample_indices(total, max_frames))
    ]


def image_batch_to_pngs(image: Any, run_dir: Path, *, max_frames: int, max_side: int) -> list[Path]:
    return save_frames(image, run_dir, max_frames=max_frames, max_side=max_side, prefix="image")


# ----------------------------------------------------------------------------- video
def _iter_stream_frames(video: Any, wanted: list[int]) -> Iterator[tuple[int, Image.Image]]:
    """Decode a file-backed VideoInput sequentially and yield only the wanted frame indices.
    Memory stays bounded to the sampled frames; never materializes the whole clip."""
    import av  # ComfyUI dependency

    src = video.get_stream_source()
    start, duration = 0.0, 0.0
    try:
        start, duration = video.get_active_trim_window()
    except Exception:
        pass
    want = set(wanted)
    last = max(wanted)
    end_t = (start + duration) if duration and duration > 0 else None
    with av.open(src, mode="r") as container:
        stream = next(s for s in container.streams if s.type == "video")
        stream.thread_type = "AUTO"
        if start > 0 and stream.time_base:
            container.seek(int(start / float(stream.time_base)), stream=stream, backward=True, any_frame=False)
        idx = 0
        for frame in container.decode(stream):
            t = frame.time
            if start > 0 and t is not None and t < start - 1e-6:
                continue
            if end_t is not None and t is not None and t > end_t + 1e-6:
                break
            if idx in want:
                yield idx, frame.to_image()
            idx += 1
            if idx > last:
                break


def _is_in_memory_video(video: Any) -> bool:
    # VideoFromComponents already holds the tensor; streaming it would re-encode the whole clip.
    return type(video).__name__ == "VideoFromComponents" or any(
        k.endswith("__components") for k in vars(video).keys()
    ) if hasattr(video, "__dict__") else False


def video_to_frame_pngs(video: Any, run_dir: Path, *, max_frames: int, max_side: int) -> tuple[list[Path], dict]:
    """ComfyUI VideoInput -> sampled frame PNGs + metadata dict."""
    paths: list[Path] = []
    meta: dict = {}
    if not _is_in_memory_video(video) and hasattr(video, "get_stream_source"):
        try:
            total = int(video.get_frame_count())
            try:
                fps = float(video.get_frame_rate())
            except Exception:
                fps = 0.0
            w, h = video.get_dimensions()
            idx = sample_indices(total, max_frames)
            decoded: list[int] = []
            for n, (i, img) in enumerate(_iter_stream_frames(video, idx)):
                paths.append(_save(img, run_dir, "video_frame", n, max_side))
                decoded.append(i)
            if paths:
                meta = _meta(total, fps, int(w), int(h), decoded, has_audio=None)
                return paths, meta
        except Exception:
            for p in paths:  # partial output from a failed stream decode; start over via components
                try:
                    p.unlink()
                except Exception:
                    pass
            paths = []

    comps = video.get_components()
    frames = comps.images
    total = int(frames.shape[0])
    try:
        fps = float(comps.frame_rate)
    except Exception:
        fps = 0.0
    h, w = int(frames.shape[1]), int(frames.shape[2])
    idx = sample_indices(total, max_frames)
    paths = save_frames(frames, run_dir, max_frames=max_frames, max_side=max_side, prefix="video_frame")
    return paths, _meta(total, fps, w, h, idx, has_audio=comps.audio is not None)


def _meta(total: int, fps: float, w: int, h: int, idx: list[int], has_audio) -> dict:
    duration = (total / fps) if fps > 0 else 0.0
    return {
        "frame_count": total,
        "fps": round(fps, 3),
        "duration_sec": round(duration, 2),
        "width": w,
        "height": h,
        "sampled_indices": idx,
        "sampled_times_sec": [round(i / fps, 2) if fps > 0 else None for i in idx],
        "has_audio": has_audio,
    }
