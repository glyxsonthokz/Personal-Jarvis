# Jarvis

A one-click installer and a set of add-ons that turn the open-source voice assistant
[Mark-LVI](https://github.com/FatihMakes/Mark-LVI) (by FatihMakes) into **Jarvis**: it can show you where to
click, use your Android phone, listen only to your voice, run standing rules on its own, show a live 3D Earth,
and keep looking at your screen when you ask it to.

> **Credit:** the assistant itself, its HUD, its tools and its design are the original author's work. This repository
> contains only the add-ons and the installer, and downloads the original at install time. See [NOTICE.md](NOTICE.md).

## Install (Windows 10/11)

1. Download or clone this repository, or take `Jarvis-Installer.zip` from a release. Extract it.
2. Double-click **`Install_Jarvis.bat`**. It installs into `%USERPROFILE%\Jarvis`, creates one **Jarvis** icon on the
   Desktop and in the Start menu, and installs Python 3.12 with winget if you have no suitable Python.
3. On first launch paste a free Gemini API key from <https://aistudio.google.com/app/apikey>.

Running the installer again **updates** everything and keeps your key, memory and settings. If you had the earlier
"Mark LVI" install, your settings come across automatically and the old icon is removed.

If something is wrong, double-click **`Diagnose_Jarvis.bat`**: it checks the install, the add-ons, the firewall and the
app's own start-up messages, and saves `Jarvis-report.txt` (API keys are never written to it).

## What it adds

| Feature | Say something like | Guide |
|---|---|---|
| **Walkthroughs** | "Walk me through changing my wallpaper" - highlights where to click, one step at a time, and types for you | `docs/READ_ME_FIRST.txt` |
| **Phone link** (Android) | "Open WhatsApp on my phone", "what notifications do I have?" | `docs/HOW_TO_LINK_YOUR_PHONE.txt` |
| **Voice lock** | "Learn my voice", "only listen to me" | `docs/HOW_TO_VOICE_LOCK_AND_AUTOMATIONS.txt` |
| **Automations** | "Close Discord whenever I open it", "focus mode", "take notes of everything I say" | same |
| **Orb HUD** | a holographic globe look for the main window (the HUD button cycles styles) | `docs/READ_ME_FIRST.txt` |
| **Earth** | "Show me the earth", "where is the ISS?", "any earthquakes today?" | `docs/HOW_TO_EARTH.txt` |
| **Screen sight** | "Keep an eye on my screen" ... "stop looking" | `docs/HOW_TO_SCREEN_SIGHT.txt` |
| **Second brain** (Obsidian + Cognee) | "Remember that ...", "what did I decide about the launch?", "open my notes in Obsidian"; spoken notes land in your vault | `docs/HOW_TO_OBSIDIAN_AND_COGNEE.txt` |

## How it works

`Install_Jarvis.bat` downloads the original Mark-LVI, copies it to `%USERPROFILE%\Jarvis`, then runs
`addons/install_addons.py`, which

* copies the files in `addons/actions` and `addons/core` into the app (the app discovers new tools by itself),
* appends short sections to the assistant's instructions (`addons/prompt_*.txt`),
* applies a handful of **small, anchored edits** to the original's files (the mic gate, the transcript hook, the frame
  sender, the HUD paint hook, the window title, the dashboard address refresh). Each edit is matched against exact
  upstream lines, is applied once, keeps the file's line endings, and is **rejected if the result would not be valid
  Python**. If an upstream change moves an anchor, the installer says so and skips that edit instead of guessing.

`tests/test_installer_patches.py` runs that whole process on a fresh download of the original, so you find out the day
an upstream change breaks a hook.

## Privacy and safety, plainly

* **Audio and screen frames go to Gemini** (Google's service), as the original assistant's audio already does. Screen
  sight only runs when asked, shows a red badge the whole time (click it to stop), starts **off** at every launch,
  skips windows whose title looks private, and sends nothing while the assistant is asleep or disconnected.
* **Voice lock** keeps your voice print (256 numbers) on your PC; speech that does not match is never sent. It is voice
  *matching*, not a security lock: a recording of you, or a very similar voice, can get through.
* **Automations** close windows politely (like clicking X), never touch Jarvis itself, Explorer, the taskbar or Task
  Manager, and "watch the screen" rules say that screenshots are being sent.
* **Second brain**: notes are plain Markdown files in your own Obsidian vault. Cognee is **off until you ask**; when on, the
  *text of your notes* goes to Gemini to build the graph (its databases stay on your PC), and notes marked private never are.
  Cognee runs in its own folder and its own Python environment (about 1.5 GB), so it cannot disturb Jarvis.
* **Phone link** never sends a message, places a call, pays, deletes, types a secret or unlocks the phone without you.
* Nothing in this repository contains keys or personal data: `config/`, `cache/`, `models/` and `tools/platform-tools/`
  are git-ignored.

## Repository layout

```
Install_Jarvis.bat        the installer (updates in place, migrates the old folder)
Diagnose_Jarvis.bat       health check  ->  diagnose.py
addons/
  install_addons.py       copies files, patches prompt/requirements/main/ui/dashboard, idempotent
  actions/*.py            the voice tools: guide_me, phone_link, voice_lock, automations, earth, screen_sight, second_brain
  core/*.py               engines: voice_gate, automations, earth_*, orb_hud, screen_sight, gui_call, vault, second_brain
  second_brain/worker.py  the process that talks to Cognee (runs in Cognee's own environment)
  prompt_*.txt            the sections added to the assistant's instructions
docs/                     one plain-text guide per feature
tests/                    run_tests.bat -> tests/run_all.py (needs the installed Jarvis; some tests download sample data)
tools/build_zip.py        builds dist/Jarvis-Installer.zip for people who just want to install
```

## Development

```
tools\build_zip.py            # python tools\build_zip.py  ->  dist\Jarvis-Installer.zip
run_tests.bat                 # runs every test against %USERPROFILE%\Jarvis
```

The tests were written and run on Linux with simulated windows, a fake phone and fake map tiles; the parts that need a
real Windows desktop (closing real windows, typing into other apps, the microphone) are exercised by hand.

## Publishing this repository

Run **`Publish_to_GitHub.bat`** (needs Git, and optionally the GitHub CLI `gh`), or follow [docs/PUBLISH.md](docs/PUBLISH.md).

## Licence

The add-ons have **no licence chosen yet** - see [NOTICE.md](NOTICE.md). The original Mark-LVI is CC BY-NC 4.0.
