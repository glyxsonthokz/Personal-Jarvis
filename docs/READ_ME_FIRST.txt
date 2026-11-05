Jarvis installer + add-ons
======================================================================================
1. Extract this whole zip (right-click > Extract All).
2. Double-click Install_Jarvis.bat.   (It installs into %USERPROFILE%\Jarvis. If you had the earlier
   "Mark LVI" install, your key, memory and settings are brought across automatically.)
3. Already installed? Run it again - it updates the app, re-applies the add-on,
   and keeps your API key and memory.

What the add-on does: when you ask how to do something ("walk me through
changing my wallpaper"), Jarvis highlights where to click, one step at a time.
The highlight stays until you click it. If a step needs text, it types it for
you after you click into the field. Say "next" to skip a step, or "where?" to
see the highlight again. It never types passwords or other secrets.

Files: addons/actions/guide_me.py is the whole feature. Delete it from
%USERPROFILE%\Jarvis\actions and the assistant goes back to normal.

PHONE LINK (new): lets Jarvis use your Android phone from the PC, and you reach Jarvis
from your phone. Read HOW_TO_LINK_YOUR_PHONE.txt - it takes about two minutes.
To remove it, delete actions\phone_link.py from %USERPROFILE%\Jarvis.

SOMETHING NOT WORKING? Double-click Diagnose_Jarvis.bat. It checks the install,
the add-ons, the phone dashboard and the firewall, starts Jarvis for about 40 seconds
to read what it says, and saves Jarvis-report.txt on your Desktop. Paste that
file's contents into the chat. API keys are never printed.

VOICE LOCK + AUTOMATIONS (new): Jarvis can respond only to your voice, and run standing
rules by itself (close apps when they open, focus mode, automatic notes, screen
watching). Read HOW_TO_VOICE_LOCK_AND_AUTOMATIONS.txt.

NEW LOOK (orb HUD): the middle of the window is now a glowing wireframe globe over a
holographic platform, in a starfield. Green when listening, amber when thinking,
orange when speaking, red when muted; the platform's edge moves with your voice.
It is applied once on install. The HUD button in the side panel cycles
face -> reactor core -> orb, and your choice is kept.

THE EARTH (new): a 3D globe on the screen that zooms into satellite maps, with live earthquakes,
the ISS, aircraft and the real day/night line. Say "show me the earth". Read HOW_TO_EARTH.txt.

SCREEN SIGHT (new): say "keep an eye on my screen" and Jarvis can see it until you say "stop looking" or
click the red badge. Read HOW_TO_SCREEN_SIGHT.txt - it explains exactly what is sent and how to stop it.
