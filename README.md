# adaptive-music-player

## Setup

Requires macOS with Homebrew.

```sh
brew install python@3.13 mpv ffmpeg
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .
cp config.example.toml config.toml
```

## Adding music

1. Set `library_path` in `config.toml` to the folder that holds your music. Subfolders are included, and the files can be `.flac`, `.mp3`, `.m4a`, `.ogg`, or `.opus`.

   To use your Apple Music library, point it at `~/Music/Music/Media/Music`. macOS protects that folder, so give your terminal app Full Disk Access first (System Settings → Privacy & Security → Full Disk Access). Apple Music subscription downloads are DRM-protected and won't appear there; purchased and imported songs will.

2. Index the library and analyze how each song sounds:

   ```sh
   adaptive-music-player scan
   ```

   Run `scan` again whenever you add, change, or remove music; removed files stop being played, and new or changed files are analyzed. The first run downloads a sound model and analyzes everything, which takes about 25 minutes for 2,600 songs. Stop it any time with Ctrl-C; the next run picks up where it left off.

   Saved analysis from versions without file-change tracking is refreshed once on the next scan. Libraries already analyzed with file-change tracking keep their cached results.

## Playing

```sh
adaptive-music-player play
```

| key | action |
|---|---|
| space | pause / resume |
| n | next song |
| p | previous song (restarts the current song if more than 3 seconds in) |
| l | like / unlike |
| ← / → | seek 10 seconds |
| q | quit |

To see how your listening is going, run:

```sh
adaptive-music-player plays
```
