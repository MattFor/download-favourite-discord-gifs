# Discord Favorite GIF Downloader

A single Python script that downloads your exported Discord favorite GIFs.

## What does it do?

- Reads the Discord favorites export JSON.
- Downloads every GIF or video source, several at a time.
- Converts video sources (MP4, WebM) to `.gif` with a two-pass palette for good colour.
- Names each file after the content it actually contains, not after the URL.
- Skips files that were already downloaded, so re-running is cheap.
- Retries temporary network errors and reports genuinely dead links separately.
- Saves everything into a `gifs/` folder.

## Requirements

- Python 3.10+
- `requests`
- `ffmpeg`, for video-to-GIF conversion

```bash
pip install -r requirements.txt
```

Without `ffmpeg` the script still runs, it just keeps video sources in their original format instead of converting them.

## Getting the JSON export from Discord

Use the snippet from this gist:

https://gist.github.com/Davr1/af6a5806a3bf4b5b7dc18829029b42c2

1. Open Discord in a browser and log in.
2. Open Developer Tools with `Ctrl + Shift + I`.
3. Go to the **Console** tab.
4. Paste the export snippet.
5. If Discord blocks pasting, type `allow pasting` first, then paste again.
6. Run the snippet.

## Usage

```bash
python download_gifs.py discord-favorite-gifs.json
```

The GIFs land in `gifs/`.

### Options

| Option              | Default | What it does                                                    |
| ------------------- | ------- | --------------------------------------------------------------- |
| `-o`, `--out DIR`   | `gifs`  | Where to save the files                                         |
| `-j`, `--workers N` | `8`     | How many downloads to run in parallel                           |
| `--timeout SECONDS` | `60`    | Per-request timeout                                             |
| `--retries N`       | `2`     | Retries for temporary errors such as timeouts and rate limits   |
| `--fps N`           | `15`    | Frame rate for converted GIFs                                   |
| `--width PX`        | `0`     | Scale converted GIFs to this width, `0` keeps the original size |
| `--keep-video`      | off     | Keep MP4/WebM sources instead of converting them                |
| `--normalize-only`  | off     | Skip downloading, only tidy up an existing folder               |
| `--no-normalize`    | off     | Skip the tidy-up pass after downloading                         |
| `--limit N`         | `0`     | Only process the first N entries, useful for a trial run        |
| `--force`           | off     | Re-download entries even if they already exist                  |

Full-size GIFs converted from video get large. If you care about disk space, `--width 480` cuts the size dramatically
for very little visible loss.

## Tidying up an existing folder

```bash
python download_gifs.py --normalize-only
```

This renames files that carry the wrong extension, converts leftover videos to GIF, and clears out interrupted
downloads. It runs automatically after every download unless `--no-normalize` is passed.

## Example output

```text
1948 entries, 311 already downloaded, 1637 to fetch.

Done.

Total entries:     1948
Skipped existing:  311
Downloaded:        1580
Converted to GIF:  1009
Renamed:           122
Dead links:        52 (3.18% of attempted)
Other failures:    5
```

## Notes

- File names keep the original favorites order through the `order` field.
- Expired Discord CDN links return HTTP 404 and are counted as dead links.

By MattFor
