"""
core/voice_gate.py — let the microphone through only when it is the owner's voice.

WHERE IT SITS
    In the mic callback, after the wake-word / echo / push-to-talk gates and just
    before a block is queued for Gemini. `feed()` answers one question for every
    64 ms block: "send this one now?". Anything the gate refuses never leaves the
    machine, the same promise push-to-talk makes.

HOW IT DECIDES
    A small speaker-recognition model (WeSpeaker ResNet34 via sherpa-onnx, ~26 MB,
    runs on the CPU) turns a second or so of speech into a 256-number "voice print".
    The print is compared with the one saved at enrollment (cosine similarity).

      IDLE    nothing said. A short ring buffer keeps the last ~0.3 s so the first
              syllable is not lost.
      HOLD    someone started talking. Audio is HELD, not sent, until ~1.2 s of
              speech (or a short utterance ends) so there is something to judge.
      OPEN    it was the owner: held audio is flushed, then everything is sent
              until ~1.2 s of silence (the model needs the silence to know a turn
              ended). Every ~2 s it re-checks, so someone else cutting in
              mid-sentence closes the gate.
      REJECT  it was not: the held audio is thrown away, and nothing is sent until
              the speaker pauses.

    The cost is about a second of latency before the assistant starts hearing a
    command. That is the price of deciding on evidence rather than on the first
    syllable.

WHAT IT IS NOT
    Voice matching, not a security lock. A recording of the owner, or a very
    similar voice, can pass; and the owner can be turned away on a very short
    word, in a noisy room, or with a cold. Strictness is adjustable, and
    status() shows the recent scores so it can be tuned against real use.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import numpy as np

SR = 16000
BLOCK_S = 1024 / SR                 # the mic callback's block (CHUNK_SIZE) in seconds
_BASE = Path(__file__).resolve().parent.parent
_MODEL_NAME = "wespeaker_en_voxceleb_resnet34.onnx"
_MODEL = _BASE / "models" / _MODEL_NAME
_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
              f"speaker-recongition-models/{_MODEL_NAME}")      # (sic) the release tag is misspelt upstream
_CFG = _BASE / "config" / "voice_lock.json"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

STRICTNESS = {"low": 0.50, "medium": 0.58, "high": 0.66}
DECIDE_S = 1.2          # speech needed before judging
MIN_S = 0.5             # shortest utterance worth judging
HANGOVER_S = 1.2        # silence that closes an OPEN gate
REVERIFY_S = 2.0        # re-check an open gate this often
PREROLL = 5             # blocks kept from before the first syllable
ENROLL_WINDOW_S = 2.0


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# ─────────────────────────────────────────────────────────────────────────────
#  Settings
# ─────────────────────────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        return json.loads(_CFG.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return {}


def _save(cfg: dict) -> None:
    _CFG.parent.mkdir(parents=True, exist_ok=True)
    _CFG.write_text(json.dumps(cfg), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
#  The model
# ─────────────────────────────────────────────────────────────────────────────
def sherpa_available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:                                         # noqa: BLE001
        return False


def ensure_sherpa() -> Optional[str]:
    """pip-install the runtime if it is missing. Returns an error string or None."""
    if sherpa_available():
        return None
    try:
        p = subprocess.run([sys.executable, "-m", "pip", "install", "sherpa-onnx"],
                           capture_output=True, timeout=300, creationflags=_NO_WINDOW)
        if p.returncode != 0:
            return (p.stderr or p.stdout).decode("utf-8", "replace").strip().splitlines()[-1][:160]
    except Exception as e:                                    # noqa: BLE001
        return str(e)
    return None if sherpa_available() else "installed, but it will not import - restart Jarvis"


def model_present() -> bool:
    return _MODEL.exists() and _MODEL.stat().st_size > 5_000_000


def ensure_model() -> Optional[str]:
    """Download the speaker model once. Returns an error string or None."""
    if model_present():
        return None
    try:
        _MODEL.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MODEL.with_suffix(".part")
        with urllib.request.urlopen(_MODEL_URL, timeout=120) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        tmp.replace(_MODEL)
        return None if model_present() else "the download was incomplete"
    except Exception as e:                                    # noqa: BLE001
        return str(e)


class _Embedder:
    def __init__(self, model_path: Path):
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=2, debug=False, provider="cpu")
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        s = self._ex.create_stream()
        s.accept_waveform(sample_rate=SR, waveform=audio.astype(np.float32))
        s.input_finished()
        return _l2(np.asarray(self._ex.compute(s), dtype=np.float32))


# ─────────────────────────────────────────────────────────────────────────────
#  The gate
# ─────────────────────────────────────────────────────────────────────────────
class VoiceGate:
    def __init__(self, embedder: Optional[Callable[[np.ndarray], np.ndarray]] = None):
        self._lock = threading.Lock()
        self._jobs: "queue.Queue" = queue.Queue()
        self._embed_fn = embedder            # tests inject one; None = load the real model lazily
        self._broken: Optional[str] = None
        cfg = _load()
        self._enabled = bool(cfg.get("enabled"))
        self._strictness = cfg.get("strictness", "medium")
        c = cfg.get("centroid")
        self._centroid = _l2(np.asarray(c, dtype=np.float32)) if c else None
        self._enrolled_windows = int(cfg.get("windows", 0))
        self._noise = 60.0
        self._loop = None
        self._q = None
        self._enroll: Optional[dict] = None
        self._on_enroll_done: Optional[Callable[[dict], None]] = None
        self.recent: deque = deque(maxlen=8)      # (clock, score, "accepted"/"rejected"/"left")
        self.accepted = 0
        self.rejected = 0
        self._reset_stream()
        threading.Thread(target=self._worker, daemon=True, name="voice-gate").start()

    # ---- state ---------------------------------------------------------
    def _reset_stream(self) -> None:
        self._state = "IDLE"
        self._pre: deque = deque(maxlen=PREROLL)
        self._hold: list = []
        self._speech_s = 0.0
        self._quiet = 0.0
        self._silence = 0.0
        self._deciding = False
        self._recent_audio: deque = deque(maxlen=25)
        self._since_verify = 0.0
        self._strikes = 0
        self._verifying = False

    @property
    def threshold(self) -> float:
        return STRICTNESS.get(self._strictness, STRICTNESS["medium"])

    def engaged(self) -> bool:
        return self._enabled and self._centroid is not None and self._broken is None

    def enrolling(self) -> bool:
        return self._enroll is not None

    # ---- the mic callback ---------------------------------------------
    def feed(self, indata, loop, out_queue) -> bool:
        """True = the caller should send this block now. Cheap: no model work here."""
        x = np.asarray(indata).reshape(-1)
        with self._lock:
            self._loop, self._q = loop, out_queue
            f = x.astype(np.float32)
            rms = float(np.sqrt(np.mean(f * f))) if f.size else 0.0
            speech = rms > max(self._noise * 3.0, 250.0)
            if not speech:
                self._noise = min(max(self._noise * 0.97 + rms * 0.03, 30.0), 800.0)

            if self._enroll is not None:                      # enrolling: collect, and let audio through
                self._collect(f, speech)
                return True
            if not self.engaged():
                return True

            blk = x.astype(np.int16).tobytes()
            st = self._state
            if st == "OPEN":
                self._recent_audio.append(f / 32768.0)
                if speech:
                    self._silence = 0.0
                    self._since_verify += BLOCK_S
                    if self._since_verify >= REVERIFY_S and not self._verifying:
                        self._verifying = True
                        self._since_verify = 0.0
                        self._jobs.put(("verify", np.concatenate(list(self._recent_audio))))
                else:
                    self._silence += BLOCK_S
                    if self._silence >= HANGOVER_S:
                        self._reset_stream()
                return True
            if st == "REJECT":
                if speech:
                    self._silence = 0.0
                else:
                    self._silence += BLOCK_S
                    if self._silence >= 0.6:
                        self._reset_stream()
                return False
            if st == "IDLE":
                self._pre.append((blk, f))
                if speech:
                    self._state = "HOLD"
                    self._hold = list(self._pre)
                    self._speech_s, self._quiet = BLOCK_S, 0.0
                return False
            # HOLD
            self._hold.append((blk, f))
            if speech:
                self._speech_s += BLOCK_S
                self._quiet = 0.0
            else:
                self._quiet += BLOCK_S
            if not self._deciding:
                if self._speech_s >= DECIDE_S or (self._quiet >= 0.4 and self._speech_s >= MIN_S):
                    self._deciding = True
                    audio = np.concatenate([a for _, a in self._hold]) / 32768.0
                    self._jobs.put(("decide", audio))
                elif self._quiet >= 0.5:                      # a click or a cough, not speech
                    self._reset_stream()
            return False

    # ---- background work ----------------------------------------------
    def _embedder(self):
        if self._embed_fn is None:
            err = None if sherpa_available() else "sherpa-onnx is not installed"
            err = err or (None if model_present() else "the speaker model is not downloaded")
            if err:
                raise RuntimeError(err)
            self._embed_fn = _Embedder(_MODEL)
        return self._embed_fn

    def _score(self, audio: np.ndarray) -> float:
        return float(self._embedder()(audio) @ self._centroid)

    def _worker(self) -> None:
        while True:
            kind, audio = self._jobs.get()
            try:
                if kind == "finish_enroll":
                    self._finish_enroll(audio)
                    continue
                score = self._score(audio)
            except Exception as e:                            # noqa: BLE001
                with self._lock:                              # fail OPEN: a broken lock must not deafen the assistant
                    self._broken = str(e)[:140]
                    self._flush_hold()
                    self._reset_stream()
                print(f"[VoiceLock] switched off: {self._broken}")
                continue
            with self._lock:
                thr = self.threshold
                if kind == "decide" and self._state == "HOLD":
                    if score >= thr:
                        self.accepted += 1
                        self.recent.append((time.strftime("%H:%M:%S"), round(score, 2), "accepted"))
                        self._flush_hold()
                        self._state, self._silence, self._since_verify = "OPEN", 0.0, 0.0
                        self._strikes, self._verifying = 0, False
                        self._recent_audio.clear()
                    else:
                        self.rejected += 1
                        self.recent.append((time.strftime("%H:%M:%S"), round(score, 2), "rejected"))
                        self._hold = []
                        self._state, self._silence, self._deciding = "REJECT", 0.0, False
                elif kind == "verify" and self._state == "OPEN":
                    self._verifying = False
                    self._strikes = self._strikes + 1 if score < thr - 0.05 else 0
                    if self._strikes >= 2:
                        self.recent.append((time.strftime("%H:%M:%S"), round(score, 2), "left"))
                        self._state, self._silence = "REJECT", 0.0

    def _flush_hold(self) -> None:
        """Send the held audio, in order, to whoever owns the queue."""
        if self._loop is None or self._q is None:
            self._hold = []
            return
        for blk, _ in self._hold:
            try:
                self._loop.call_soon_threadsafe(self._q.put_nowait, {"data": blk, "mime_type": "audio/pcm"})
            except Exception:                                 # noqa: BLE001
                break
        self._hold = []
        self._deciding = False

    # ---- enrollment ----------------------------------------------------
    def start_enroll(self, seconds: float, on_done: Callable[[dict], None]) -> None:
        with self._lock:
            self._enroll = {"need": float(seconds), "got": 0.0, "audio": [], "queued": False}
            self._on_enroll_done = on_done
            self._reset_stream()

    def cancel_enroll(self) -> None:
        with self._lock:
            self._enroll = None

    def enroll_progress(self) -> Optional[float]:
        e = self._enroll
        return None if e is None else min(1.0, e["got"] / e["need"])

    def _collect(self, f: np.ndarray, speech: bool) -> None:
        e = self._enroll
        if speech and e["got"] < e["need"]:
            e["audio"].append(f / 32768.0)
            e["got"] += BLOCK_S
        if e["got"] >= e["need"] and not e["queued"]:
            e["queued"] = True
            self._jobs.put(("finish_enroll", np.concatenate(e["audio"])))

    def _finish_enroll(self, audio: np.ndarray) -> None:
        result: dict = {"ok": False}
        try:
            n, hop = int(ENROLL_WINDOW_S * SR), int(ENROLL_WINDOW_S * SR / 2)
            embs = [self._embedder()(audio[s:s + n]) for s in range(0, max(1, len(audio) - n + 1), hop)]
            if len(embs) < 4:
                raise RuntimeError("not enough speech was captured")
            centroid = _l2(np.mean(embs, axis=0))
            within = [float(e @ centroid) for e in embs]
            with self._lock:
                self._centroid, self._enrolled_windows = centroid, len(embs)
                self._enabled, self._broken = True, None
                cfg = _load()
                cfg.update(enabled=True, strictness=self._strictness,
                           centroid=[float(v) for v in centroid], windows=len(embs))
                _save(cfg)
                self.accepted = self.rejected = 0
            result = {"ok": True, "windows": len(embs), "typical_match": round(float(np.mean(within)), 2)}
        except Exception as e:                                # noqa: BLE001
            result = {"ok": False, "error": str(e)[:140]}
        finally:
            with self._lock:
                self._enroll = None
                cb = self._on_enroll_done
            if cb:
                try:
                    cb(result)
                except Exception:                             # noqa: BLE001
                    pass

    # ---- settings ------------------------------------------------------
    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self._enabled = bool(on)
            self._reset_stream()
            cfg = _load()
            cfg["enabled"] = self._enabled
            _save(cfg)

    def set_strictness(self, level: str) -> None:
        with self._lock:
            self._strictness = level if level in STRICTNESS else "medium"
            cfg = _load()
            cfg["strictness"] = self._strictness
            _save(cfg)

    def forget(self) -> None:
        with self._lock:
            self._centroid, self._enabled, self._enrolled_windows = None, False, 0
            self._reset_stream()
            try:
                _CFG.unlink()
            except OSError:
                pass

    def status(self) -> dict:
        with self._lock:
            return {"enabled": self._enabled, "enrolled": self._centroid is not None,
                    "strictness": self._strictness, "threshold": self.threshold,
                    "accepted": self.accepted, "rejected": self.rejected,
                    "recent": list(self.recent), "problem": self._broken,
                    "model_ready": model_present() and sherpa_available()}

    def clear_problem(self) -> None:
        with self._lock:
            self._broken = None
            self._embed_fn = None


# ─────────────────────────────────────────────────────────────────────────────
#  Module-level helpers (what main.py's patched mic callback calls)
# ─────────────────────────────────────────────────────────────────────────────
_gate: Optional[VoiceGate] = None
_gate_lock = threading.Lock()


def get_gate() -> VoiceGate:
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = VoiceGate()
        return _gate


_boot_checked = False


def active() -> bool:
    """Cheap check for the mic callback (it runs ~15x a second): is there anything
    for the gate to do? Reads the settings file once, not once per block."""
    global _boot_checked
    if _gate is None:
        if _boot_checked:
            return False
        _boot_checked = True
        cfg = _load()
        if not (cfg.get("enabled") and cfg.get("centroid")):
            return False
        get_gate()
    return _gate.engaged() or _gate.enrolling()


def feed(indata, loop, out_queue) -> bool:
    return get_gate().feed(indata, loop, out_queue)
