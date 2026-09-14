import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import mpv

from adaptive_music_player.events.log import Event, liked_state, record_event
from adaptive_music_player.model.picker import pick_random
from adaptive_music_player.player import macos, terminal

TICK_S = 0.1
SEEK_STEP_MS = 10_000
RESTART_THRESHOLD_MS = 3_000
PROGRESS_INTERVAL_MS = 5_000
MAX_LISTEN_STEP_MS = 1_500
SESSION_GAP = timedelta(minutes=30)
MAX_CONSECUTIVE_ERRORS = 5


class PlaybackStopped(Exception):
    """Playback cannot continue; the message explains why."""


@dataclass
class Play:
    id: str
    song: sqlite3.Row
    listened_ms: float = 0
    anchor_ms: float | None = None
    last_progress_ms: float = 0


class Player:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        macos.hide_dock_icon()
        self.mpv = mpv.MPV(vid="no", keep_open="yes", input_default_bindings=False,
                           input_vo_keyboard=False, ytdl=False)
        self.file_loaded = False
        self.load_failed = False
        self.mpv.event_callback("file-loaded")(lambda _event: setattr(self, "file_loaded", True))
        self.mpv.event_callback("end-file")(self._on_end_file)

        self.play: Play | None = None
        self.history: list[Play] = []
        self.liked = False
        self.errors = 0
        self.session_id, self.last_activity = self._resume_session()

    def run(self) -> None:
        self._play_pick()
        with terminal.raw_terminal():
            terminal.print_line(terminal.HELP)
            while True:
                for action in terminal.read_keys(TICK_S):
                    if action == "quit":
                        return
                    getattr(self, f"_on_{action}")()
                self._tick()
                self._render()

    def close(self) -> None:
        self.mpv.terminate()

    def _tick(self) -> None:
        if self.load_failed:
            self._record("error")
            terminal.print_line(f"cannot play {self.play.song['path']}")
            self.errors += 1
            if self.errors >= MAX_CONSECUTIVE_ERRORS:
                raise PlaybackStopped(f"{self.errors} songs in a row failed to play")
            self._play_pick(keep_history=False)
            return
        if not self.file_loaded:
            return
        self.errors = 0
        self._update_listening()
        if self.mpv.eof_reached:
            self._record("ended")
            self._play_pick()
        elif self.play.listened_ms - self.play.last_progress_ms >= PROGRESS_INTERVAL_MS:
            self.play.last_progress_ms = self.play.listened_ms
            self._record("progress")

    def _play_pick(self, keep_history: bool = True) -> None:
        current = self.play
        if current is not None and keep_history:
            self.history.append(current)
        pick = pick_random(self.conn, current.song["id"] if current else None)
        if pick is None:
            raise PlaybackStopped("no playable songs; run `adaptive-music-player scan`")
        self._start(self._song(pick.song_id), "automatic", pick_id=pick.id)

    def _start(self, song: sqlite3.Row, reason: str, **links: str) -> None:
        self.play = Play(id=str(uuid.uuid4()), song=song)
        self.liked = liked_state(self.conn, song["id"])
        self.file_loaded = self.load_failed = False
        self.mpv.loadfile(song["path"])
        self.mpv.pause = False
        self._record("start", position_ms=0, duration_ms=song["duration_ms"],
                     liked=self.liked, reason=reason, **links)

    def _update_listening(self) -> None:
        """Add continuous forward playback since the last observation."""
        position_s = self.mpv.time_pos
        if position_s is None or self.mpv.seeking:
            self.play.anchor_ms = None
            return
        position_ms = position_s * 1000
        anchor = self.play.anchor_ms
        if anchor is not None and not self.mpv.pause and 0 < position_ms - anchor <= MAX_LISTEN_STEP_MS:
            self.play.listened_ms += position_ms - anchor
        self.play.anchor_ms = position_ms

    # Key actions

    def _on_pause(self) -> None:
        self._update_listening()
        self.mpv.pause = not self.mpv.pause
        self._record("pause" if self.mpv.pause else "resume")

    def _on_next(self) -> None:
        self._update_listening()
        self._record("next")
        self._play_pick()

    def _on_previous(self) -> None:
        self._update_listening()
        departing = self.play
        restart = self._position_ms() > RESTART_THRESHOLD_MS or not self.history
        self._record("previous")
        if restart:
            self._start(departing.song, "restart", navigation_from_play_id=departing.id,
                        replay_of_play_id=departing.id)
        else:
            earlier = self.history.pop()
            self._start(earlier.song, "previous", navigation_from_play_id=departing.id,
                        replay_of_play_id=earlier.id)

    def _on_like(self) -> None:
        self.liked = not self.liked
        self._record("like" if self.liked else "unlike")

    def _on_forward(self) -> None:
        self._seek(SEEK_STEP_MS)

    def _on_back(self) -> None:
        self._seek(-SEEK_STEP_MS)

    def _seek(self, offset_ms: int) -> None:
        if not self.file_loaded:
            return
        self._update_listening()
        from_ms = self._position_ms()
        to_ms = min(max(0, from_ms + offset_ms), self.play.song["duration_ms"])
        self.mpv.seek(to_ms / 1000, reference="absolute", precision="exact")
        self.play.anchor_ms = None
        self._record("seek", position_ms=to_ms, seek_from_ms=from_ms, seek_to_ms=to_ms)

    # Helpers

    def _on_end_file(self, event) -> None:
        if event.data.reason == mpv.MpvEventEndFile.ERROR:
            self.load_failed = True

    def _record(self, type_: str, position_ms: int | None = None, **fields) -> None:
        now = datetime.now(UTC)
        if now - self.last_activity > SESSION_GAP:
            self.session_id = str(uuid.uuid4())
        self.last_activity = now
        record_event(self.conn, Event(
            session_id=self.session_id, play_id=self.play.id, song_id=self.play.song["id"],
            type=type_, occurred_at=now,
            position_ms=self._position_ms() if position_ms is None else position_ms,
            listened_total_ms=round(self.play.listened_ms), **fields))

    def _resume_session(self) -> tuple[str, datetime]:
        row = self.conn.execute(
            "SELECT session_id, occurred_at FROM events ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            return row["session_id"], datetime.fromisoformat(row["occurred_at"])
        return str(uuid.uuid4()), datetime.now(UTC)

    def _position_ms(self) -> int:
        return round((self.mpv.time_pos or 0) * 1000) if self.file_loaded else 0

    def _song(self, song_id: int) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT id, path, title, artist, album, duration_ms FROM songs WHERE id = ?",
            (song_id,)).fetchone()

    def _render(self) -> None:
        song = self.play.song
        terminal.show_status(
            f"{'⏸' if self.mpv.pause else '▶'} {song['title']} — {song['artist']} · {song['album']}"
            f"   {terminal.format_ms(self._position_ms())} / {terminal.format_ms(song['duration_ms'])}"
            f"{'  ♥' if self.liked else ''}")
