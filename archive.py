#!/usr/bin/env python3
"""
archive.py - Recursively archive image and video media in a directory.
    Compresses videos to H265 + OPUS in MKV, and images to JPEG-XL.
    The following stream formats will not be re-encoded:
        Video:  H265, AV1
        Audio:  Opus
        Images: JPEG-XL, AVIF
    Other stream formats will be re-encoded appropriately.
    Metadata is preserved where possible. All videos are remuxed to MKV. AVIF
    images will not be modified, only copied to the output directory.
    If during compression the output file already exists, the file will be
    skipped and logged as an error.

Required system binaries, must be in PATH:
    ffmpeg, ffprobe - video/audio inspection and encoding
    cjxl            - JPEG-XL image encoding, from libjxl
    exiftool        - metadata copy for non-JPEG image sources

Logs are written to the current working directory:
    success.log  - "<input> -> <output> [COMPRESSED|COPY] [VIDEO|IMAGE]"
    error.log    - error messages with the relevant input path
    compress.log - verbose ffmpeg/ffprobe/cjxl output
"""

import argparse
import itertools
import json
import logging
import multiprocessing
import queue
import shutil
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

from tqdm import tqdm

# Required external binaries
REQUIRED_BINARIES = ["ffmpeg", "ffprobe", "cjxl", "exiftool"]

# Recognised file extensions for video and image files
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts",
}
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".webp", ".heic", ".heif", ".avif", ".jxl",
}


# Hardware acceleration
class HWAccel(NamedTuple):
    decode_flags: list[str]
    encoder: str
    quality_flags: list[str]


# Hardware acceleration arguments
HWACCEL_MAP: dict[str, HWAccel] = {
    "none":  HWAccel([], "libx265", ["-crf", "28", "-preset", "medium"]),
    "nvidia":HWAccel(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"], "hevc_nvenc", ["-rc", "vbr", "-cq", "28", "-preset", "p4"]),
    "amd":   HWAccel([],"hevc_amf", ["-quality", "balanced", "-qp_i", "28", "-qp_p", "28"]),
    "intel": HWAccel(["-hwaccel", "qsv"],"hevc_qsv", ["-global_quality", "28", "-preset", "medium"]),
}


# Passthrough codecs and formats that will not be re-encoded
PASSTHROUGH_VIDEO_CODECS = {"hevc", "av1"}
PASSTHROUGH_AUDIO_CODECS = {"opus"}
PASSTHROUGH_IMAGE_FORMATS = {"jxl", "avif"}


# ID output file
RESUME_DATA_FILE = "resume.txt"


# Binary presence check
def check_required_binaries() -> None:
    """Abort with a message if any required tool is missing."""
    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing:
        sys.exit(
            "error: missing required tool(s): " + ", ".join(missing) + "\n"
            "  Please install the missing binaries and ensure they are in your system PATH."
        )


# Atomic ID counter
class AtomicCounter:
    """Thread-safe counter."""

    def __init__(self, start: int = 0):
        self._counter = itertools.count(start)
        self._lock = threading.Lock()
        self._current = start - 1

    def next(self) -> int:
        with self._lock:
            self._current = next(self._counter)
            return self._current

    @property
    def value(self) -> int:
        with self._lock:
            return self._current


def format_id(n: int, id_format: str) -> str:
    """8-digit zero-padded ID. id_format is either 'dec' or 'hex'."""
    return f"{n:08x}" if id_format == "hex" else f"{n:08d}"


def parse_id(s: str, id_format: str) -> int:
    """Parse an ID string in the given format ('dec' or 'hex') into an integer."""
    base = 16 if id_format == "hex" else 10
    return int(s, base)


# Thread-safe stats
class Stats:
    """
    A thread-safe counter for the processing outcome of different media types.
    In context, counts the number of compressed, copied, and erroneous files
    encountered for both videos and images.
    """

    def __init__(self):
        self._counts = Counter()
        self._lock = threading.Lock()

    def increment(self, media_type: str, outcome: str) -> None:
        """Increment the outcome count for a media type.

        Args:
            media_type : str
                video|image|other
            outcome : str
                compressed|copied|error
        """
        with self._lock:
            self._counts[(media_type, outcome)] += 1

    def summary_lines(self) -> list[str]:
        """
        Return a text summary of the current outcome counts.
        """
        with self._lock:
            c = dict(self._counts)
        lines = []
        for media in ("video", "image"):
            compressed = c.get((media, "compressed"), 0)
            copied = c.get((media, "copy"), 0)
            errors = c.get((media, "error"), 0)
            lines.append(
                f"  {media.capitalize():<6} - compressed: {compressed}, "
                f"copied: {copied}, errors: {errors}"
            )
        skipped = c.get(("other", "skipped"), 0)
        if skipped:
            lines.append(f"  Skipped (unrecognised): {skipped}")
        return lines


# Logging setup
def setup_logging(log_dir: Path):
    """
    Independent file loggers, silenced from stdout/stderr so only the
    progress bars and final summary appear on the terminal.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.root.setLevel(logging.CRITICAL)

    def file_logger(name: str, path: Path, level: int) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(level)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    success  = file_logger("success",  log_dir / "success.log",  logging.INFO)
    error    = file_logger("error",    log_dir / "error.log",     logging.WARNING)
    compress = file_logger("compress", log_dir / "compress.log",  logging.DEBUG)
    return success, error, compress


# Image format detection using magic bytes
def detect_image_format(path: Path) -> str:
    """Sniff real image format from file header, ignoring the extension."""
    try:
        with open(path, "rb") as f:
            header = f.read(32)
    except OSError:
        return "unknown"

    if header[:2] == b"\xff\xd8":
        return "jpeg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if header[:2] == b"BM":
        return "bmp"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "heic"
    if header[:2] == b"\xff\x0a":  # raw JXL codestream
        return "jxl"
    if header[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":  # ISOBMFF JXL container
        return "jxl"
    return "unknown"


# Codec extraction using ffprobe
def probe_av_codecs(path: Path, compress_log: logging.Logger) -> tuple[str | None, str | None, bool]:
    """
    Returns (video_codec, audio_codec). 
    If probing fails, returns (None, None, False).
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "stream=codec_name,codec_type",
        "-of", "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        compress_log.debug(f"[probe] {path}\n{result.stdout}\n{result.stderr}")
        streams = json.loads(result.stdout).get("streams", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        compress_log.debug(f"[probe error] {path}: {exc}")
        return None, None, False

    v_codec = a_codec = None
    for s in streams:
        if s.get("codec_type") == "video" and v_codec is None:
            v_codec = s.get("codec_name", "").lower()
        elif s.get("codec_type") == "audio" and a_codec is None:
            a_codec = s.get("codec_name", "").lower()
    return v_codec, a_codec, True


# Detect black frames
def probe_black_frames(path: Path, compress_log: logging.Logger,
                       min_duration: float = 0.1,
                       pixel_threshold: float = 0.1) -> tuple[float | None, float | None]:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "frame_tags=lavfi.black_start,lavfi.black_end",
        "-of", "json",
        "-f", "lavfi",
        f"amovie={path}:s=v,blackdetect=d={min_duration}:pix_th={pixel_threshold}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        compress_log.debug(f"[blackdetect] {path}\n{result.stdout}\n{result.stderr}")
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        compress_log.debug(f"[blackdetect error] {path}: {exc}")
        return None, None

    # black_start and black_end arrive on separate frames — collect independently
    starts = []
    ends = []
    for frame in data.get("frames", []):
        tags = frame.get("tags", {})
        if "lavfi.black_start" in tags:
            starts.append(float(tags["lavfi.black_start"]))
        if "lavfi.black_end" in tags:
            ends.append(float(tags["lavfi.black_end"]))

    if not starts:
        compress_log.debug(f"[blackdetect] {path}: no black segments detected")
        return None, None

    # Pair starts and ends into segments; guard against an unpaired trailing start
    segments = list(zip(starts, ends)) if ends else [(starts[0], None)]

    content_start = segments[0][1] if segments[0][0] < 0.5 and segments[0][1] is not None else None
    content_end   = segments[-1][0] if len(segments) > 1 or content_start is None else None

    compress_log.debug(f"[blackdetect] {path}: content_start={content_start}, content_end={content_end}")
    return content_start, content_end


# Image and video encoding
def encode_video(src: Path, dst: Path, video_ok: bool, audio_ok: bool,
                 has_audio: bool, hw: HWAccel,
                 trim_start: float | None, trim_end: float | None,
                 compress_log: logging.Logger) -> bool:
    """
    Re-encode only the required streams.
    hw selects software or hardware encoder + decode flags.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    needs_trim = trim_start is not None or trim_end is not None

    if needs_trim:
        v_flags = ["-c:v", hw.encoder, *hw.quality_flags]
        a_flags = ["-c:a", "libopus", "-b:a", "128k"] if has_audio else []
    else:
        v_flags = ["-c:v", "copy"] if video_ok else \
                  ["-c:v", hw.encoder, *hw.quality_flags]
        if not has_audio:
            a_flags = []
        elif audio_ok:
            a_flags = ["-c:a", "copy"]
        else:
            a_flags = ["-c:a", "libopus", "-b:a", "128k"]

    trim_flags = []
    if trim_start is not None:
        trim_flags += ["-ss", str(trim_start)]
    if trim_end is not None:
        trim_flags += ["-to", str(trim_end)]

    cmd = [
        "ffmpeg", "-y",
        *hw.decode_flags,
        "-i", str(src),
        *trim_flags,
        "-map", "0", "-dn",
        "-map_metadata", "0", "-map_chapters", "0",
        *v_flags, *a_flags,
        "-c:s", "copy",
        str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        compress_log.debug(f"[encode] {src} [{trim_start} - {trim_end}] -> {dst}\n{result.stdout}\n{result.stderr}")
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        compress_log.debug(f"[encode error] {src} [{trim_start} - {trim_end}]: {exc}")
        return False


def encode_image(src: Path, dst: Path, fmt: str, compress_log: logging.Logger) -> bool:
    """
    Encode to JPEG-XL using cjxl.

    Uses high-quality lossy compression for non-JPEG sources, and lossless for
    JPEG sources.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jpeg":
        cmd = ["cjxl", str(src), str(dst), "--effort", "7", "--num_threads=0"]
    else:
        cmd = ["cjxl", str(src), str(dst), "-q", "90", "--effort", "7", "--num_threads=0"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)
        compress_log.debug(f"[cjxl] {src} -> {dst}\n{result.stdout}\n{result.stderr}")
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        compress_log.debug(f"[cjxl error] {src}: {exc}")
        return False


# Path helpers
def copy_metadata(src: Path, dst: Path, compress_log: logging.Logger) -> bool:
    """
    Copy all metadata from src to dst using exiftool.
    Used for non-JPEG image sources where cjxl does not guarantee EXIF/XMP
    preservation.
    Returns True on success.
    """
    cmd = [
        "exiftool",
        "-overwrite_original",  # no _original backup file left behind
        "-preserve",  # preserve dst file timestamps
        "-TagsFromFile", str(src),  # copy all metadata tags from src
        "-All:All",  # copy every tag group verbatim
        str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        compress_log.debug(f"[exiftool] {src} -> {dst}\n{result.stdout}\n{result.stderr}")
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        compress_log.debug(f"[exiftool error] {src}: {exc}")
        return False


def mirror_path(src: Path, input_root: Path, output_root: Path,
                new_stem: str, new_suffix: str) -> Path:
    """
    Given a source file path, input root, and output root, return the
    corresponding output path with the same relative directory structure,
    but with a new stem and suffix.
    """
    rel_dir = src.relative_to(input_root).parent
    return output_root / rel_dir / f"{new_stem}{new_suffix}"


# Synchronous file worker
def process_file(
    src: Path, input_root: Path, output_root: Path,
    counter: AtomicCounter | None, id_format: str,
    hw: HWAccel,
    trim_black: bool,
    success_log: logging.Logger, error_log: logging.Logger,
    compress_log: logging.Logger, stats: Stats,
) -> None:
    ext = src.suffix.lower()
    is_video = ext in VIDEO_EXTENSIONS
    is_image = ext in IMAGE_EXTENSIONS

    if not (is_video or is_image):
        error_log.warning(f"[skip] not a recognised video or image file: {src}")
        stats.increment("other", "skipped")
        return

    stem = format_id(counter.next(), id_format) if counter is not None else src.stem
    media = "video" if is_video else "image"

    if is_video:
        dst = mirror_path(src, input_root, output_root, stem, ".mkv")
    else:
        dst = mirror_path(src, input_root, output_root, stem, ".jxl")

    if dst.exists():
        error_log.warning(f"[exists] output already exists, skipping: {dst}")
        stats.increment(media, "error")
        return

    # Video
    if is_video:
        v_codec, a_codec, ok = probe_av_codecs(src, compress_log)
        if not ok:
            error_log.warning(f"[probe failed] could not determine codecs: {src}")
            stats.increment("video", "error")
            return

        video_ok = v_codec in PASSTHROUGH_VIDEO_CODECS if v_codec else False
        has_audio = a_codec is not None
        audio_ok = (not has_audio) or (a_codec in PASSTHROUGH_AUDIO_CODECS)

        trim_start, trim_end = probe_black_frames(src, compress_log) if trim_black else (None, None)
        needs_trim = trim_start is not None or trim_end is not None

        dst.parent.mkdir(parents=True, exist_ok=True)
        if video_ok and audio_ok and not needs_trim:
            ok = encode_video(src, dst, True, True, has_audio, hw,
                              None, None, compress_log)
            if ok:
                success_log.info(f"{src} -> {dst} [COPY] [VIDEO]")
                stats.increment("video", "copy")
            else:
                error_log.warning(f"[remux failed] {src}")
                stats.increment("video", "error")
        else:
            ok = encode_video(src, dst, video_ok, audio_ok, has_audio, hw,
                              trim_start, trim_end, compress_log)
            if ok:
                action = "TRIMMED" if needs_trim and video_ok and audio_ok else "COMPRESSED"
                success_log.info(f"{src} -> {dst} [{action}] [VIDEO]")
                stats.increment("video", "compressed")
            else:
                error_log.warning(f"[encode failed] {src}")
                stats.increment("video", "error")
        return

    # Image
    fmt = detect_image_format(src)
    if fmt == "unknown":
        error_log.warning(f"[probe failed] unrecognised image format: {src}")
        stats.increment("image", "error")
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    if fmt in PASSTHROUGH_IMAGE_FORMATS:
        try:
            shutil.copy2(src, dst if fmt == "jxl" else dst.with_suffix(f".{fmt}"))
            actual_dst = dst if fmt == "jxl" else dst.with_suffix(f".{fmt}")
            success_log.info(f"{src} -> {actual_dst} [COPY] [IMAGE]")
            stats.increment("image", "copy")
        except OSError as exc:
            error_log.warning(f"[copy failed] {src}: {exc}")
            stats.increment("image", "error")
    else:
        ok = encode_image(src, dst, fmt, compress_log)
        if ok:
            if fmt != "jpeg" and not copy_metadata(src, dst, compress_log):
                error_log.warning(f"[metadata] exiftool copy failed: {src}")
            success_log.info(f"{src} -> {dst} [COMPRESSED] [IMAGE]")
            stats.increment("image", "compressed")
        else:
            error_log.warning(f"[encode failed] {src}")
            stats.increment("image", "error")


# File traversal
def collect_files(input_root: Path) -> list[Path]:
    return sorted(p for p in input_root.rglob("*") if p.is_file())


# Command-line arg parsing
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Archive videos (H265/AV1+Opus/MKV) and images (JPEG-XL)."
    )
    p.add_argument("input_dir", type=Path, help="Source directory root")
    p.add_argument("output_dir", type=Path, help="Destination directory root, auto-created")
    p.add_argument("--idname", action="store_true",
                    help="Replace filenames with sequential IDs")
    p.add_argument("--idstart", type=str, default=None,
                    help="Starting ID (format per --idformat); implies --idname")
    p.add_argument("--idformat", choices=["dec", "hex"],
                    help="Format of --idstart and generated IDs (default: dec)")
    p.add_argument("--workers", type=int, default=2,
                    help="Parallel worker threads (default 2; CPU-bound)")
    p.add_argument("--accel", choices=["none", "nvidia", "amd", "intel"], default="none",
                    help="Hardware video encoder/decoder to use (default: none)")
    p.add_argument("--trimblack", action="store_true",
                    help="Trim black frames from start/end of videos (default: off)")
    p.add_argument("--idout", action="store_true",
                    help=f"Outputs the next id and current format to '{RESUME_DATA_FILE}' in the current directory. Implies --idname.")
    p.add_argument("--idresume", action="store_true",
                    help=f"Resume ID from '{RESUME_DATA_FILE}' in the current directory. Implies --idname.")
    return p.parse_args()


def read_saved_format_and_id(file: Path) -> tuple[str, int]:
    with open(file, "r", encoding="utf-8") as f:
        format, start_val = f.read().strip().split()
    format = format.lower()
    print("format", format)

    if format not in ("dec", "hex"):
        raise ValueError(f"Invalid format '{format}' in '{file}'; expected 'dec' or 'hex'.")

    return format, parse_id(start_val, format)


def write_saved_format_and_id(file: Path, format: str, id_value: int) -> None:
    with open(file, "w", encoding="utf-8") as f:
        f.write(f"{format.lower()} {format_id(id_value, format)}")


def resolve_id_and_format(args: argparse.Namespace) -> int:
    if args.idresume and args.idstart:
        sys.exit("error: --idresume and --idstart cannot be used together; use --idresume alone to resume from id.txt.")
    if args.idresume and args.idformat:
        sys.exit("error: --idresume and --idformat cannot be used together; use --idresume alone to resume from id.txt.")

    if args.idresume:
        args.idname = True
        id_file_path = Path.cwd() / RESUME_DATA_FILE
        if not id_file_path.exists():
            sys.exit(f"error: --idresume specified but '{RESUME_DATA_FILE}' does not exist in the current directory.")
        try:
            args.idformat, start_val = read_saved_format_and_id(id_file_path)
        except (OSError, ValueError) as exc:
            sys.exit(f"error: failed to read starting ID from '{RESUME_DATA_FILE}': {exc}")
    elif args.idstart is not None:
        args.idname = True
        base = 16 if args.idformat == "hex" else 10
        try:
            start_val = int(args.idstart, base)
        except ValueError:
            sys.exit(f"error: --idstart '{args.idstart}' is not valid {args.idformat}")
    else:
        start_val = 0

    args.idformat = args.idformat or "dec"

    if args.idname:
        print(
            f"[id] Interpreting IDs as {args.idformat.upper()}; "
            f"starting ID = {start_val} ({format_id(start_val, args.idformat)})"
        )
    return start_val


# Main entry
def main():
    check_required_binaries()
    args = parse_args()
    start_val = resolve_id_and_format(args)

    input_root  = args.input_dir.resolve()
    output_root = args.output_dir.resolve()

    if not input_root.is_dir():
        sys.exit(f"error: input directory does not exist: {input_root}")

    output_root.mkdir(parents=True, exist_ok=True)
    success_log, error_log, compress_log = setup_logging(Path.cwd())

    files = collect_files(input_root)
    if not files:
        print("No files found — nothing to do.")
        return
    
    if (args.idout and not args.idname):
        sys.exit("error: --idout requires --idname to be specified or implied")

    counter = AtomicCounter(start_val) if args.idname else None
    stats = Stats()
    hw = HWACCEL_MAP[args.accel]
    print(f"[accel] Video encoder: {hw.encoder} ({'hardware' if args.accel != 'none' else 'software'})")

    trim_black = args.trimblack

    # Progress bars
    overall_bar = tqdm(total=len(files), desc="Overall", position=0, unit="file")
    slot_queue: queue.Queue[int] = queue.Queue()
    for i in range(args.workers):
        slot_queue.put(i)
    worker_bars = [
        tqdm(total=1, position=i + 1, bar_format="{desc}", leave=False)
        for i in range(args.workers)
    ]
    for bar in worker_bars:
        bar.set_description_str(f"[Worker {worker_bars.index(bar) + 1}] idle")

    def run_task(src: Path):
        slot = slot_queue.get()
        bar = worker_bars[slot]
        bar.set_description_str(f"[Worker {slot + 1}] {src.name}")
        try:
            process_file(
                src, input_root, output_root, counter, args.idformat,
                hw, trim_black, success_log, error_log, compress_log, stats,
            )
        finally:
            bar.set_description_str(f"[Worker {slot + 1}] idle")
            slot_queue.put(slot)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_task, src): src for src in files}
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc:
                error_log.warning(f"[unexpected error] {futures[fut]}: {exc}")
                compress_log.debug(f"[unexpected error traceback] {futures[fut]}", exc_info=exc)
                stats.increment("other", "skipped")
            overall_bar.update(1)

    for bar in worker_bars:
        bar.close()
    overall_bar.close()

    print("\nFinished.")

    if counter is not None:
        if args.idout:
            id_out_path = Path.cwd() / RESUME_DATA_FILE
            try:
                write_saved_format_and_id(id_out_path, args.idformat, counter.next())
                print(f"[id] Final ID {counter.value} written to {id_out_path}")
            except OSError as exc:
                error_log.warning(f"[id] Failed to write final id {id_out_path}: {exc}")
        else:
            print(f"[id] Final ID used: {counter.value}")

    for line in stats.summary_lines():
        print(line)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
