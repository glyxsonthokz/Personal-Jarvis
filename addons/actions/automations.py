"""
actions/automations.py — standing instructions: "whenever X, do Y" without being asked again.

The engine lives in core/automations.py and runs in the background for as long as
Jarvis does; rules are saved and come back after a restart. This file is the voice
interface to it, and starting the engine is its only side effect on import.
"""
from __future__ import annotations

import os
import sys

from core import outcome
from core import automations as au

try:                                  # rules saved last time start working the moment Jarvis launches
    au.engine().boot()
except Exception as _e:               # noqa: BLE001
    print(f"[Automations] could not start: {_e}")


def _list(e) -> str:
    rules = [au.describe(r) for r in e.rules]
    ev = e.events[-5:]
    return (outcome.done("automations", rules=len(rules), recent_events=len(ev)) +
            ("\n" + "\n".join(rules) if rules else "\n(no automations set up)") +
            ("\nRecent: " + " | ".join(ev) if ev else ""))


def automations(parameters: dict = None, player=None, session_memory=None) -> str:
    p = parameters or {}
    e = au.engine()
    if player is not None:
        e.player = player
    action = str(p.get("action") or "list").strip().lower()
    minutes = int(float(p.get("minutes") or 0))
    force = bool(p.get("force"))

    if action == "list":
        return _list(e)
    if action in ("remove", "delete", "stop", "cancel"):
        which = str(p.get("which") or p.get("rule") or "").strip()
        if not which:
            return outcome.ask("automations", "Which one should I remove? Say the name of the rule, or 'all'.")
        if which in ("notes", "note"):
            which = "notes"
        n = e.remove(which)
        return outcome.done("automations", removed=n) if n else outcome.failed("automations", f"no rule matches '{which}'")
    if action in ("pause", "resume"):
        which = str(p.get("which") or "all")
        n = e.set_enabled(which, action == "resume")
        return outcome.done("automations", **{action + "d": n}) if n else outcome.failed("automations", f"no rule matches '{which}'")

    if action in ("block", "focus", "watch") and not e.supported():
        return outcome.failed("automations", "watching windows only works on Windows for now")

    if action == "block":
        apps, titles = p.get("apps"), p.get("titles")
        if not (apps or titles):
            return outcome.ask("automations", "Which apps or sites should I close when they open?")
        r = e.add_block(apps, titles, force=force, include_running=bool(p.get("include_running")), minutes=minutes)
        return outcome.done("automations", rule=r["id"], blocking=", ".join(r["apps"] + r["titles"]),
                            lasts=f"{minutes} min" if minutes else "until removed") + (
            " -- Apps already open right now are left alone; only new ones are closed"
            + ("" if r["include_running"] else " (say so if you also want the open ones closed)")
            + ". Windows get a normal polite close first, so unsaved work asks to be saved. "
              "Tell the user in one short sentence.")
    if action == "focus":
        allow = p.get("allow") or p.get("apps")
        if not allow:
            return outcome.ask("automations", "Which apps are allowed during focus mode?")
        r = e.add_focus(allow, minutes=minutes, force=force)
        return outcome.done("automations", rule=r["id"], allowed=", ".join(r["allow"]),
                            lasts=f"{minutes} min" if minutes else "until removed") + (
            " -- Anything NEW that is not on that list gets closed; what is open now stays. "
              "Tell the user in one short sentence, and how to end it ('stop focus mode').")
    if action == "notes_start":
        if e.active("notes"):
            return outcome.failed("automations", "notes are already being taken")
        r = e.add_notes(quiet=p.get("quiet", True) is not False, all_voices=bool(p.get("all_voices")))
        return outcome.done("automations", rule=r["id"], writing_to=r["file"], quiet=r["quiet"]) + (
            " -- NOTE-TAKING IS ON. Say one short sentence that you are writing everything down, then "
            "stay COMPLETELY silent: your voice is muted while it runs, so do not answer or act on "
            "anything you hear. Only do something if the user tells you to stop or pause the notes."
            + (" Only the enrolled voice is written down unless they asked for every voice." if not r["all_voices"] else ""))
    if action == "notes_stop":
        path, _ = e.notes_tail(1)
        n = e.set_enabled("notes", False)
        return (outcome.done("automations", notes_stopped=True, file=path) + " -- Tell the user the notes "
                "are saved and where; offer to open or summarise them.") if n else outcome.failed("automations", "no notes are being taken")
    if action in ("notes_open", "notes_read", "notes_summary"):
        path, tail = e.notes_tail(int(p.get("lines") or 80))
        if not path:
            return outcome.failed("automations", "there are no notes yet")
        if action == "notes_open":
            try:
                (os.startfile(path) if sys.platform == "win32" else None)          # noqa: S606
            except Exception as ex:                                              # noqa: BLE001
                return outcome.failed("automations", f"could not open the notes: {ex}", file=path)
            return outcome.done("automations", opened=path)
        return outcome.done("automations", file=path) + " " + outcome.untrusted("the spoken notes", tail or "(empty)")
    if action == "watch":
        cond = str(p.get("condition") or "").strip()
        if not cond:
            return outcome.ask("automations", "What should I look for on your screen?")
        r = e.add_watch(cond, int(p.get("interval") or 30), str(p.get("do") or "say"), str(p.get("message") or ""))
        return outcome.done("automations", rule=r["id"], looking_for=cond, every=f"{r['interval']}s", then=r["action"]) + (
            " -- PRIVACY: while this is on, a small screenshot of the screen is sent to Gemini every "
            f"{r['interval']} seconds. Say that plainly in one sentence, and how to turn it off.")
    return outcome.failed("automations", f"unknown action '{action}'")


TOOL = {
    "name": "automations",
    "description": (
        "STANDING INSTRUCTIONS that run by themselves in the background, without the user asking each "
        "time. Use when they set up a rule: 'close Discord whenever I open it', 'block YouTube', "
        "'focus mode - only let me use Word and Chrome', 'don't let me open any other apps for an "
        "hour', 'take notes of everything I say', 'write down this meeting', 'tell me if I'm on "
        "Instagram', 'stop focus mode', 'what rules do I have?'. Rules are saved and survive restarts. "
        "Actions: 'block' (apps = names, titles = words in a window title such as a website, "
        "minutes = optional duration, force = hard-kill stubborn apps, include_running = also close what "
        "is already open), 'focus' (allow = the only apps permitted, minutes), 'notes_start' (quiet, "
        "all_voices) / 'notes_stop' / 'notes_open' / 'notes_summary', 'watch' (condition = what to look "
        "for on screen, interval seconds, do = say | log | close_window, message), 'list', 'remove' "
        "(which = rule name or 'all' or 'focus'/'block_app'/'notes'/'watch'), 'pause', 'resume'. "
        "Closing is polite (like clicking X) so unsaved work asks to be saved; Jarvis itself, Explorer, "
        "the taskbar and Task Manager are never touched. 'watch' sends screenshots to Gemini - say so. "
        "While notes run your voice is muted: stay silent until the user says to stop. Windows only."
    ),
    "brief": "Standing rules: close apps on open, focus mode, continuous note-taking, screen watching.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "What to do.",
                       "enum": ["block", "focus", "notes_start", "notes_stop", "notes_open", "notes_summary",
                                "watch", "list", "remove", "pause", "resume"]},
            "apps": {"type": "STRING", "description": "App names, comma separated: 'discord, steam'."},
            "titles": {"type": "STRING", "description": "Words in a window title, comma separated: 'youtube, instagram'."},
            "allow": {"type": "STRING", "description": "For 'focus': the only apps allowed, comma separated."},
            "minutes": {"type": "NUMBER", "description": "How long the rule lasts; omit for until removed."},
            "force": {"type": "BOOLEAN", "description": "Hard-kill apps that ignore a polite close (can lose unsaved work)."},
            "include_running": {"type": "BOOLEAN", "description": "For 'block': also close matching apps that are open right now."},
            "quiet": {"type": "BOOLEAN", "description": "For 'notes_start': stay silent while taking notes (default true)."},
            "all_voices": {"type": "BOOLEAN", "description": "For 'notes_start': write down every voice, not just the enrolled one."},
            "condition": {"type": "STRING", "description": "For 'watch': what to look for on the screen, in plain words."},
            "interval": {"type": "NUMBER", "description": "For 'watch': seconds between looks (minimum 15, default 30)."},
            "do": {"type": "STRING", "description": "For 'watch': what to do when seen.",
                   "enum": ["say", "log", "close_window"]},
            "message": {"type": "STRING", "description": "For 'watch': what to say or log."},
            "which": {"type": "STRING", "description": "For 'remove'/'pause'/'resume': a rule name, a rule type, or 'all'."},
            "lines": {"type": "NUMBER", "description": "For 'notes_summary': how many recent lines to read."},
        },
        "required": ["action"],
    },
    "handler": automations,
}
