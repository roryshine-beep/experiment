"""Pure-stdlib MIDI -> chiptune WAV renderer.

Dominion is stdlib-only with no audio dependencies, and this machine has no MIDI
synth/soundfont — so to play a .mid we parse it ourselves and synthesize a simple
square-wave ("8-bit") rendition to a WAV, which `aplay`/`paplay` can then loop.
The retro timbre is a feature: it suits the NES-overworld tile view.

Only the common subset is handled: format 0/1, PPQN division, note on/off,
tempo meta. Aftertouch/CC/pitch-bend/program/sysex are skipped (they don't affect
a square-wave render). The result is baked once and cached (see render_cached).
"""
import math
import os
import struct
import wave

SR = 22050          # sample rate (mono) — plenty for a square-wave render
VOICE_AMP = 0.16    # per-voice amplitude; sum of voices is soft-clipped at write
ATTACK = 0.004      # envelope attack (s) — kills click on note start
RELEASE = 0.05      # envelope release (s) — tail so notes don't cut harshly


def _vlq(data, i):
    """Read a MIDI variable-length quantity at data[i]; return (value, next_i)."""
    val = 0
    while True:
        b = data[i]
        i += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            return val, i


def _parse_track(data):
    """Parse one MTrk body into absolute-tick events: (tick, kind, a, b).
    kind: 'on'/'off' (a=note, b=vel) or 'tempo' (a=us_per_quarter)."""
    events = []
    i, tick, status = 0, 0, 0
    n = len(data)
    while i < n:
        dt, i = _vlq(data, i)
        tick += dt
        b0 = data[i]
        if b0 & 0x80:
            status = b0
            i += 1
        # else: running status — b0 is the first data byte, reuse `status`
        hi = status & 0xF0
        if hi in (0x80, 0x90):                      # note off / note on
            note = data[i]; vel = data[i + 1]; i += 2
            if hi == 0x90 and vel > 0:
                events.append((tick, "on", note, vel))
            else:
                events.append((tick, "off", note, 0))
        elif hi in (0xA0, 0xB0, 0xE0):              # aftertouch / CC / pitch bend
            i += 2
        elif hi in (0xC0, 0xD0):                    # program change / chan pressure
            i += 1
        elif status == 0xFF:                        # meta event
            mtype = data[i]; i += 1
            length, i = _vlq(data, i)
            if mtype == 0x51:                       # set tempo (us per quarter note)
                us = int.from_bytes(data[i:i + 3], "big")
                events.append((tick, "tempo", us, 0))
            i += length
        elif status in (0xF0, 0xF7):                # sysex — skip its payload
            length, i = _vlq(data, i)
            i += length
        else:
            break                                   # unknown — bail on this track
    return events


def parse_midi(path):
    """Return (notes, total_seconds) where notes is a list of (start_s, end_s,
    note, vel), by merging all tracks and walking the tempo map."""
    with open(path, "rb") as f:
        buf = f.read()
    if buf[:4] != b"MThd":
        raise ValueError("not a Standard MIDI File")
    division = struct.unpack(">H", buf[12:14])[0]
    if division & 0x8000:
        raise ValueError("SMPTE time division not supported")
    tpq = division or 480                           # ticks per quarter note

    # Slice out each MTrk chunk and parse it, then merge on absolute tick.
    merged = []
    i = 14
    while i + 8 <= len(buf):
        cid = buf[i:i + 4]
        clen = struct.unpack(">I", buf[i + 4:i + 8])[0]
        body = buf[i + 8:i + 8 + clen]
        i += 8 + clen
        if cid == b"MTrk":
            merged.extend(_parse_track(body))
    # tempo first, then note-off before note-on, at any shared tick
    order = {"tempo": 0, "off": 1, "on": 2}
    merged.sort(key=lambda e: (e[0], order[e[1]]))

    notes, active = [], {}
    cur_tick, cur_s, us_per_q = 0, 0.0, 500000      # default 120 bpm
    spt = (us_per_q / 1e6) / tpq                     # seconds per tick
    for tick, kind, a, b in merged:
        cur_s += (tick - cur_tick) * spt
        cur_tick = tick
        if kind == "tempo":
            us_per_q = a
            spt = (us_per_q / 1e6) / tpq
        elif kind == "on":
            active.setdefault(a, []).append((cur_s, b))
        elif kind == "off":
            stack = active.get(a)
            if stack:
                start_s, vel = stack.pop(0)
                notes.append((start_s, cur_s, a, vel))
    total = max((e for _, e, _, _ in notes), default=0.0)
    return notes, total


def render(notes, total_seconds, sr=SR):
    """Synthesize the notes into a list of float samples (square waves + a short
    attack/release envelope), summed and left for soft-clipping at write time."""
    n_total = int((total_seconds + RELEASE) * sr) + 1
    buf = [0.0] * n_total
    a_len = max(1, int(ATTACK * sr))
    r_len = max(1, int(RELEASE * sr))
    for start_s, end_s, note, vel in notes:
        freq = 440.0 * 2 ** ((note - 69) / 12.0)
        amp = VOICE_AMP * (vel / 127.0)
        s0 = int(start_s * sr)
        hold = max(1, int((end_s - start_s) * sr))
        span = hold + r_len
        inc = freq / sr
        phase = 0.0
        for k in range(span):
            if k < a_len:
                env = k / a_len
            elif k < hold:
                env = 1.0
            else:
                env = 1.0 - (k - hold) / r_len
            idx = s0 + k
            if 0 <= idx < n_total:
                buf[idx] += amp * env if phase < 0.5 else -amp * env
            phase += inc
            if phase >= 1.0:
                phase -= 1.0
    return buf


def write_wav(buf, path, sr=SR):
    """Soft-clip the float buffer to 16-bit PCM and write a mono WAV."""
    frames = bytearray()
    for v in buf:
        # tanh soft-clip keeps dense chords from harsh digital clipping
        v = math.tanh(v)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def render_cached(midi_path, wav_path=None):
    """Bake midi_path -> a cached WAV (next to it, .wav), re-rendering only when
    missing or older than the MIDI. Returns the WAV path, or None on failure."""
    if wav_path is None:
        wav_path = os.path.splitext(midi_path)[0] + ".wav"
    try:
        if (os.path.exists(wav_path)
                and os.path.getmtime(wav_path) >= os.path.getmtime(midi_path)):
            return wav_path
        notes, total = parse_midi(midi_path)
        if not notes:
            return None
        write_wav(render(notes, total), wav_path)
        return wav_path
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    import time
    src = sys.argv[1]
    t0 = time.monotonic()
    notes, total = parse_midi(src)
    out = os.path.splitext(src)[0] + ".wav"
    write_wav(render(notes, total), out)
    print(f"{len(notes)} notes, {total:.1f}s -> {out} "
          f"({os.path.getsize(out)} bytes) in {time.monotonic() - t0:.1f}s")
