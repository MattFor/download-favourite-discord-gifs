#!/usr/bin/env python3

from __future__ import annotations

import re
import sys
import json
import time
import shutil
import argparse
import subprocess
import concurrent.futures as futures

from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import requests
import requests.adapters

MEDIA_EXTS = (
	".jpeg",
	".webm",
	".webp",
	".avif",
	".mp4",
	".gif",
	".png",
	".jpg",
	".mov",
	".mkv",
	".bin",
)

VIDEO_EXTS = (".mp4", ".webm", ".mov", ".mkv")

# Magic-byte signatures
SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
	(b"GIF87a", 0, ".gif"),
	(b"GIF89a", 0, ".gif"),
	(b"\x89PNG\r\n\x1a\n", 0, ".png"),
	(b"\xff\xd8\xff", 0, ".jpg"),
	(b"\x1a\x45\xdf\xa3", 0, ".webm"),
	(b"ftyp", 4, ".mp4"),
	(b"RIFF", 0, ".webp"),
)

# Responses that mean the link is gone for good
DEAD_STATUS = frozenset({400, 401, 403, 404, 410, 451})

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

print_lock = Lock()


def log(message: str, *, err: bool = False) -> None:
	with print_lock:
		print(message, file=sys.stderr if err else sys.stdout, flush=True)


class DeadLink(Exception):
	"""The source is permanently unavailable."""


def safe_name(text: str, max_len: int = 80) -> str:
	text = re.sub(r"[^\w.\-]+", "_", text.strip())
	return text[:max_len].strip("._-") or "file"


def strip_media_ext(name: str) -> str:
	"""Remove every trailing media extension: 'a.webp.gif' -> 'a'."""
	changed = True
	while changed:
		changed = False
		lowered = name.lower()
		for ext in MEDIA_EXTS:
			if lowered.endswith(ext) and len(name) > len(ext):
				name = name[: -len(ext)]
				changed = True
				break
	return name


def stem_for(page_url: str, order: int) -> str:
	basename = urlparse(page_url).path.rstrip("/").split("/")[-1]
	return f"{order:06d}_{safe_name(strip_media_ext(basename) or 'gif')}"


def sniff_ext(path: Path) -> str | None:
	"""Return the real extension of a file, or None if it is not media."""
	try:
		with open(path, "rb") as f:
			head = f.read(64)
	except OSError:
		return None

	if len(head) < 12:
		return None

	for magic, offset, ext in SIGNATURES:
		if head[offset : offset + len(magic)] == magic:
			if ext == ".webp" and head[8:12] != b"WEBP":
				continue
			return ext

	return None


def have_ffmpeg() -> bool:
	return shutil.which("ffmpeg") is not None


def run_ffmpeg(args: list[str]) -> None:
	result = subprocess.run(
		["ffmpeg", "-y", "-nostdin", "-loglevel", "error", *args],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.PIPE,
		text=True,
		check=False,
	)

	if result.returncode != 0:
		detail = (result.stderr or "").strip().splitlines()
		message = detail[-1] if detail else f"exit code {result.returncode}"
		raise RuntimeError(f"ffmpeg failed: {message}")


def video_to_gif(video_path: Path, gif_path: Path, fps: int, width: int) -> None:
	palette = video_path.with_name(video_path.name + ".palette.png")
	temp_gif = gif_path.with_name(gif_path.name + ".part")
	scale = f"scale={width}:-1:flags=lanczos" if width > 0 else "scale=iw:-1:flags=lanczos"

	try:
		run_ffmpeg(
			[
				"-i",
				str(video_path),
				"-vf",
				f"fps={fps},{scale},palettegen",
				"-f",
				"image2",
				str(palette),
			]
		)
		run_ffmpeg(
			[
				"-i",
				str(video_path),
				"-i",
				str(palette),
				"-lavfi",
				f"fps={fps},{scale}[x];[x][1:v]paletteuse",
				"-loop",
				"0",
				"-f",
				"gif",
				str(temp_gif),
			]
		)

		if not temp_gif.exists() or temp_gif.stat().st_size == 0:
			raise RuntimeError("ffmpeg produced an empty file")

		temp_gif.replace(gif_path)
	finally:
		palette.unlink(missing_ok=True)
		temp_gif.unlink(missing_ok=True)


def download_file(
	session: requests.Session,
	url: str,
	referer: str,
	out_path: Path,
	*,
	timeout: int,
	retries: int,
) -> None:
	headers = {
		"User-Agent": (
			"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
		),
		"Referer": referer,
		"Accept": "image/gif,image/webp,image/*,video/*;q=0.9,*/*;q=0.8",
	}

	temp_path = out_path.with_name(out_path.name + ".part")
	last_error: Exception | None = None

	for attempt in range(retries + 1):
		try:
			with session.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
				if r.status_code in DEAD_STATUS:
					raise DeadLink(f"HTTP {r.status_code}")

				if r.status_code in RETRY_STATUS:
					raise requests.HTTPError(f"HTTP {r.status_code}")

				r.raise_for_status()

				with open(temp_path, "wb") as f:
					for chunk in r.iter_content(chunk_size=256 * 1024):
						if chunk:
							f.write(chunk)

			if temp_path.stat().st_size == 0:
				raise DeadLink("empty response")

			temp_path.replace(out_path)
			return

		except DeadLink:
			temp_path.unlink(missing_ok=True)
			raise
		except (requests.RequestException, OSError) as e:
			last_error = e
			temp_path.unlink(missing_ok=True)

			if attempt < retries:
				time.sleep(1.5 * (2**attempt))

	raise requests.RequestException(str(last_error))


class Stats:
	def __init__(self) -> None:
		self.lock = Lock()
		self.downloaded = 0
		self.converted = 0
		self.skipped = 0
		self.dead = 0
		self.failed = 0
		self.renamed = 0

	def bump(self, field: str, amount: int = 1) -> None:
		with self.lock:
			setattr(self, field, getattr(self, field) + amount)


def process_entry(
	session: requests.Session,
	page_url: str,
	src: str,
	stem: str,
	out_dir: Path,
	stats: Stats,
	args: argparse.Namespace,
	ffmpeg_ok: bool,
	index: int,
	total: int,
) -> None:
	staging = out_dir / f"{stem}.download"

	try:
		log(f"[{index}/{total}] {src}")
		download_file(
			session,
			src,
			page_url,
			staging,
			timeout=args.timeout,
			retries=args.retries,
		)

		real_ext = sniff_ext(staging)

		if real_ext is None:
			raise DeadLink("response was not a media file")

		if real_ext in VIDEO_EXTS and args.convert and ffmpeg_ok:
			final_path = out_dir / f"{stem}.gif"
			video_to_gif(staging, final_path, args.fps, args.width)
			stats.bump("converted")
		else:
			final_path = out_dir / f"{stem}{real_ext}"
			staging.replace(final_path)

		stats.bump("downloaded")
		log(f"    saved {final_path.name}")

	except DeadLink as e:
		stats.bump("dead")
		log(f"    dead link: {src} ({e})", err=True)
	except requests.RequestException as e:
		stats.bump("failed")
		log(f"    network failure: {src} ({e})", err=True)
	except Exception as e:
		stats.bump("failed")
		log(f"    failed: {src} ({e})", err=True)
	finally:
		staging.unlink(missing_ok=True)


def normalize_dir(out_dir: Path, stats: Stats, args: argparse.Namespace, ffmpeg_ok: bool) -> None:
	for path in sorted(out_dir.iterdir()):
		if not path.is_file():
			continue

		if path.name.endswith((".part", ".download", ".palette.png")):
			path.unlink(missing_ok=True)
			continue

		real_ext = sniff_ext(path)

		if real_ext is None:
			log(f"Not a media file: {path.name}", err=True)
			continue

		key = strip_media_ext(path.name)

		if real_ext in VIDEO_EXTS and args.convert and ffmpeg_ok:
			target = out_dir / f"{key}.gif"

			if target.exists():
				log(
					f"Leaving {path.name} alone, {target.name} already exists.",
					err=True,
				)
				continue

			try:
				video_to_gif(path, target, args.fps, args.width)
				path.unlink(missing_ok=True)
				stats.bump("converted")
				log(f"Converted: {path.name} -> {target.name}")
			except Exception as e:
				log(f"Could not convert {path.name}: {e}", err=True)

			continue

		target = out_dir / f"{key}{real_ext}"

		if target == path:
			continue

		if target.exists():
			log(
				f"Skipping rename, target exists: {path.name} -> {target.name}",
				err=True,
			)
			continue

		path.rename(target)
		stats.bump("renamed")
		log(f"Renamed: {path.name} -> {target.name}")


def load_entries(json_path: Path) -> list[tuple[str, str, str]]:
	with open(json_path, encoding="utf-8") as f:
		data = json.load(f)

	if not isinstance(data, dict):
		raise ValueError("expected the JSON root to be an object")

	entries: list[tuple[str, str, str]] = []
	used: dict[str, int] = {}

	for i, (page_url, meta) in enumerate(data.items(), start=1):
		if not isinstance(meta, dict):
			log(f"Skipping entry {i}: metadata is not an object", err=True)
			continue

		src = meta.get("src")

		if not isinstance(src, str) or not src.strip():
			log(f"Skipping entry {i}: missing src", err=True)
			continue

		try:
			order = int(meta.get("order", i))
		except (TypeError, ValueError):
			log(
				f"Entry {i}: unusable order {meta.get('order')!r}, using position",
				err=True,
			)
			order = i

		stem = stem_for(page_url, order)
		count = used.get(stem, 0)
		used[stem] = count + 1

		if count:
			stem = f"{stem}-{count + 1}"

		entries.append((page_url, src.strip(), stem))

	return entries


def existing_keys(out_dir: Path) -> set[str]:
	return {
		strip_media_ext(p.name)
		for p in out_dir.iterdir()
		if p.is_file() and not p.name.endswith((".part", ".download"))
	}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Download Discord favorite GIFs from an exported JSON file.",
	)

	parser.add_argument("json_file", nargs="?", help="Path to the Discord favorites JSON export")

	parser.add_argument("-o", "--out", default="gifs", help="Output directory (default: ./gifs)")

	parser.add_argument("-j", "--workers", type=int, default=8, help="Parallel downloads (default: 8)")

	parser.add_argument(
		"--timeout",
		type=int,
		default=60,
		help="Per-request timeout in seconds (default: 60)",
	)

	parser.add_argument(
		"--retries",
		type=int,
		default=2,
		help="Retries for transient errors (default: 2)",
	)

	parser.add_argument("--fps", type=int, default=15, help="GIF frame rate (default: 15)")

	parser.add_argument(
		"--width",
		type=int,
		default=0,
		help="Scale GIFs to this width, 0 keeps the original",
	)

	parser.add_argument(
		"--keep-video",
		dest="convert",
		action="store_false",
		help="Keep MP4/WebM sources instead of converting them to GIF",
	)

	parser.add_argument(
		"--normalize-only",
		action="store_true",
		help="Skip downloading and only tidy up the existing output directory",
	)

	parser.add_argument(
		"--no-normalize",
		dest="normalize",
		action="store_false",
		help="Skip the tidy-up pass after downloading",
	)

	parser.add_argument("--limit", type=int, default=0, help="Only process the first N entries")

	parser.add_argument("--force", action="store_true", help="Re-download entries that already exist")

	args = parser.parse_args(argv)

	if not args.normalize_only and not args.json_file:
		parser.error("json_file is required unless --normalize-only is given")

	if args.workers < 1:
		parser.error("--workers must be at least 1")

	if args.fps < 1:
		parser.error("--fps must be at least 1")

	return args


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv)

	out_dir = Path(args.out).expanduser().resolve()
	out_dir.mkdir(parents=True, exist_ok=True)

	stats = Stats()
	ffmpeg_ok = have_ffmpeg()

	if args.convert and not ffmpeg_ok:
		log(
			"ffmpeg was not found, video sources will be kept in their original format.",
			err=True,
		)

	if args.normalize_only:
		normalize_dir(out_dir, stats, args, ffmpeg_ok)
		log(f"\nRenamed {stats.renamed} file(s), converted {stats.converted}.")
		return 0

	json_path = Path(args.json_file).expanduser().resolve()

	if not json_path.is_file():
		log(f"JSON file not found: {json_path}", err=True)
		return 1

	try:
		entries = load_entries(json_path)
	except (ValueError, json.JSONDecodeError) as e:
		log(f"Could not read {json_path}: {e}", err=True)
		return 1

	if not entries:
		log("No usable entries in the JSON file.", err=True)
		return 1

	if args.limit:
		entries = entries[: args.limit]

	have = set() if args.force else existing_keys(out_dir)
	pending = [e for e in entries if e[2] not in have]
	stats.skipped = len(entries) - len(pending)

	log(f"{len(entries)} entries, {stats.skipped} already downloaded, {len(pending)} to fetch.\n")

	if pending:
		session = requests.Session()
		adapter = requests.adapters.HTTPAdapter(pool_connections=args.workers, pool_maxsize=args.workers)
		session.mount("https://", adapter)
		# noinspection HttpUrlsUsage
		session.mount("http://", adapter)

		total = len(pending)

		try:
			with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
				jobs = [
					pool.submit(
						process_entry,
						session,
						page_url,
						src,
						stem,
						out_dir,
						stats,
						args,
						ffmpeg_ok,
						i,
						total,
					)
					for i, (page_url, src, stem) in enumerate(pending, start=1)
				]

				for job in futures.as_completed(jobs):
					job.result()
		except KeyboardInterrupt:
			log("\nInterrupted, partial files cleaned up on the next run.", err=True)
			return 130
		finally:
			session.close()

	if args.normalize:
		log("\nTidying up file names...")
		normalize_dir(out_dir, stats, args, ffmpeg_ok)

	attempted = len(pending)
	dead_pct = (stats.dead / attempted * 100) if attempted else 0.0

	log("\nDone.\n")
	log(f"Total entries:     {len(entries)}")
	log(f"Skipped existing:  {stats.skipped}")
	log(f"Downloaded:        {stats.downloaded}")
	log(f"Converted to GIF:  {stats.converted}")
	log(f"Renamed:           {stats.renamed}")
	log(f"Dead links:        {stats.dead} ({dead_pct:.2f}% of attempted)")
	log(f"Other failures:    {stats.failed}")

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
