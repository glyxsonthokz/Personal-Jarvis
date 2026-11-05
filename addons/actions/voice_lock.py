"""
actions/voice_lock.py — teach the assistant YOUR voice, and have it ignore everyone else.

Enrolling takes about 20 seconds of the user talking. After that the microphone
gate in core/voice_gate.py only forwards speech that matches. See that file for
how it decides and what it cannot promise: it is voice matching, not a lock.

This tool never listens by itself. Enrolling reuses the microphone stream the app
already has open; the gate collects the user's speech from it for ~20 seconds,
then builds the voice print on-device. The audio is not saved - only the print
(256 numbers) is, in config/voice_lock.json.
"""
from __future__ import annotations

import threading

from core import outcome
from core import voice_gate as vg

_ENROLL_SECONDS = 20


def _say(player, text: str) -> None:
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(text)
        except Exception:                                     # noqa: BLE001
            pass


def _enroll_thread(player) -> None:
    gate = vg.get_gate()
    err = vg.ensure_sherpa()
    if not err:
        err = vg.ensure_model()
    if err:
        _say(player, "[VOICE_LOCK] Setting up the voice lock FAILED: "
                     f"{err[:120]}. Tell the user in one short sentence that it needs an internet "
                     "connection the first time, and to try again.")
        return
    gate.clear_problem()

    def done(result: dict) -> None:
        if result.get("ok"):
            _say(player, "[VOICE_LOCK] Enrolment finished and the voice lock is now ON. Tell the user "
                         "in one or two short sentences: it will only listen to their voice from now "
                         "on, and they can say 'turn off the voice lock' any time. Mention that it is "
                         "voice matching, not a security lock.")
        else:
            _say(player, "[VOICE_LOCK] Enrolment did not work: "
                         f"{result.get('error', 'unknown')}. Tell the user briefly, suggest a quieter "
                         "spot, and offer to try again.")

    gate.start_enroll(_ENROLL_SECONDS, done)
    _say(player, f"[VOICE_LOCK] Ready. Ask the user, in one short sentence, to keep talking naturally "
                 f"for about {_ENROLL_SECONDS} seconds - reading something aloud works well. Then stay "
                 "COMPLETELY silent and do not answer what they say; you will get a message when it is done.")


def voice_lock(parameters: dict = None, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower()
    gate = vg.get_gate()

    if action in ("enroll", "enrol", "learn", "train"):
        if gate.enrolling():
            return outcome.failed("voice_lock", "already listening for the enrolment - the user should keep talking")
        threading.Thread(target=_enroll_thread, args=(player,), daemon=True, name="voice-enroll").start()
        first = not vg.model_present() or not vg.sherpa_available()
        return outcome._line(outcome.STARTED, "voice_lock", {
            "enrolling": True, "first_time_setup": first,
            "seconds": _ENROLL_SECONDS}) + (
            " -- Tell the user in one short sentence that you are getting ready"
            + (" (the first time this downloads about 26 MB)" if first else "")
            + " and wait: you will get a [VOICE_LOCK] message telling you when to ask them to talk.")

    if action in ("on", "enable"):
        if not gate.status()["enrolled"]:
            return outcome.failed("voice_lock", "no voice is enrolled yet - run action='enroll' first")
        gate.clear_problem()
        gate.set_enabled(True)
        return outcome.done("voice_lock", enabled=True)
    if action in ("off", "disable"):
        gate.set_enabled(False)
        return outcome.done("voice_lock", enabled=False)
    if action == "strictness":
        level = str(p.get("level") or "").strip().lower()
        if level not in vg.STRICTNESS:
            return outcome.failed("voice_lock", "level must be low, medium or high")
        gate.set_strictness(level)
        return outcome.done("voice_lock", strictness=level, threshold=vg.STRICTNESS[level]) + (
            " -- Higher means fewer strangers get through but you are turned away more often.")
    if action in ("forget", "delete", "reset"):
        gate.forget()
        return outcome.done("voice_lock", forgot_voice_print=True)

    s = gate.status()
    return outcome.done(
        "voice_lock", enabled=s["enabled"], enrolled=s["enrolled"], strictness=s["strictness"],
        accepted=s["accepted"], rejected=s["rejected"],
        problem=s["problem"] or "none", ready=s["model_ready"],
        recent_scores=", ".join(f"{t} {v} {r}" for t, v, r in s["recent"][-4:]) or "none yet",
    ) + (" -- If the user says it keeps ignoring THEM, suggest strictness=low or enrolling again in the "
         "room they normally use; if it lets others in, suggest high.")


TOOL = {
    "name": "voice_lock",
    "description": (
        "VOICE LOCK: makes the assistant respond ONLY to the user's own voice and ignore everyone else "
        "(other people talking, a TV, a video). Use when they say things like 'only listen to me', "
        "'learn my voice', 'ignore other voices', 'turn on / off voice lock', 'it keeps ignoring me'. "
        "Actions: 'enroll' (first time, or to redo it: about 20 seconds of the user talking - when you "
        "get the [VOICE_LOCK] ready message, ask them to keep talking, then say nothing), 'on', 'off', "
        "'strictness' (level low/medium/high), 'forget' (deletes the saved voice print), 'status'. "
        "Be honest if asked: it matches voices, it is not a security lock - a recording of them or a "
        "very similar voice can get through, and they may be ignored on a one-word command or in a noisy "
        "room. Speech from anyone else is never sent to the cloud while it is on."
    ),
    "brief": "Responds only to the user's own voice; learns it in about 20 seconds.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "What to do.",
                       "enum": ["enroll", "on", "off", "strictness", "forget", "status"]},
            "level": {"type": "STRING", "description": "For 'strictness': low, medium or high.",
                      "enum": ["low", "medium", "high"]},
        },
        "required": ["action"],
    },
    "handler": voice_lock,
}
