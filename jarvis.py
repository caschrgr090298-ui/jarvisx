"""
╔══════════════════════════════════════════════════════════════╗
║          J.A.R.V.I.S  v3  —  Full PC AI Voice Assistant     ║
║                                                              ║
║  ✅ Voice commands  (wake-word "Jarvis" like Alexa)          ║
║  ✅ Text-to-speech voice                                     ║
║  ✅ Full PC control  (keyboard, mouse, shell)                ║
║  ✅ Find & open files anywhere on the PC                     ║
║  ✅ Open browser + navigate URLs                             ║
║  ✅ Keyboard control  (type, hotkeys, key presses)           ║
║  ✅ Mouse control  (move, click, drag, scroll)               ║
║  ✅ Open any installed app                                   ║
║  ✅ Learning mode — teach by voice OR by watching actions    ║
║  ✅ GUI app window  (tabbed dark-theme control panel)        ║
║  ✅ Runs as Administrator  (auto UAC elevation)              ║
║  ✅ Auto-starts when Windows boots  (registry)               ║
║  ✅ Only responds after hearing "Jarvis"  (wake word)        ║
║  ✅ CODING ENGINE — write, run, debug, explain, save code    ║
╚══════════════════════════════════════════════════════════════╝

SETUP:  double-click setup.bat  →  set ANTHROPIC_API_KEY  →  python jarvis.py

BUGS FIXED IN THIS VERSION:
  - speak() called _log_line() before it was defined → moved _log_line above speak()
  - pyttsx3 deadlock on multi-threaded speak() → dedicated TTS thread with queue
  - mouse.unhook(cb) wrong API → replaced with mouse.unhook_all() + re-hook guard
  - open_code_in_editor used "notepad++" as command → correct exe paths
  - Compiled language temp .exe files not cleaned up → added finally cleanup
  - pyautogui.write() breaks on Unicode → clipboard paste fallback via pyperclip
  - Anthropic client init at module level with bad key → lazy init with guard
  - is_in_startup() swallowed all exceptions → only catches FileNotFoundError
  - _code_tab_ref None race condition → safe callable guard before use
  - API max_tokens too low for large code → raised to 4096
"""

# ─── stdlib ────────────────────────────────────────────────────────────────────
import os, sys, json, time, glob, threading, re, tempfile, queue
import subprocess, webbrowser, winreg, ctypes, logging, urllib.parse
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
from datetime import datetime

# ─── third-party ───────────────────────────────────────────────────────────────
import speech_recognition as sr
import pyttsx3, pyautogui, psutil, keyboard, mouse
from anthropic import Anthropic

try:
    import pyperclip      # pip install pyperclip  — used for Unicode typing
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
WAKE_WORD         = "jarvis"
VOICE_RATE        = 172
VOICE_VOLUME      = 1.0
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
LEARN_FILE        = os.path.join(BASE_DIR, "jarvis_memory.json")
CODE_DIR          = os.path.join(BASE_DIR, "jarvis_code")
APP_NAME          = "JARVIS"
STARTUP_REG_KEY   = r"Software\Microsoft\Windows\CurrentVersion\Run"
SCRIPT_PATH       = os.path.abspath(sys.argv[0])

os.makedirs(CODE_DIR, exist_ok=True)
pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.05

logging.basicConfig(
    filename=os.path.join(BASE_DIR, "jarvis.log"),
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s"
)

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING HELPER  — defined FIRST so everything below can use it
# ══════════════════════════════════════════════════════════════════════════════

_gui_log_widget = None   # set later when GUI builds

def _log_line(msg: str):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}]  {msg}"
    print(line)
    logging.info(msg)
    if _gui_log_widget:
        try:
            _gui_log_widget.configure(state="normal")
            _gui_log_widget.insert(tk.END, line + "\n")
            _gui_log_widget.see(tk.END)
            _gui_log_widget.configure(state="disabled")
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
#  TTS — thread-safe via a queue + dedicated speaker thread
#  FIX: pyttsx3.runAndWait() is not re-entrant; calling from multiple threads
#       causes deadlocks on Windows. All speak calls go through a queue.
# ══════════════════════════════════════════════════════════════════════════════

_tts_queue: queue.Queue = queue.Queue()

def _get_voice_id(engine) -> str | None:
    """Return a male voice ID, or None to keep default."""
    for v in engine.getProperty("voices"):
        if any(n in v.name.lower() for n in ("david", "mark", "male", "george")):
            return v.id
    return None

def _tts_worker():
    """
    Single background thread that owns pyttsx3.
    FIX: pyttsx3 on Windows corrupts its internal COM state after the first
    runAndWait() call in a non-main thread. Reinitialising the engine for
    every utterance is ugly but 100% reliable.
    """
    while True:
        text = _tts_queue.get()
        if text is None:
            break
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate",   VOICE_RATE)
            engine.setProperty("volume", VOICE_VOLUME)
            vid = _get_voice_id(engine)
            if vid:
                engine.setProperty("voice", vid)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception as e:
            logging.error(f"TTS error: {e}")
            # Last-resort: try Windows SAPI directly via subprocess
            try:
                subprocess.run(
                    ["powershell", "-Command",
                     f'Add-Type -AssemblyName System.Speech; '
                     f'$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                     f'$s.Speak("{text.replace(chr(34), "")}");'],
                    timeout=15, capture_output=True
                )
            except Exception:
                pass
        finally:
            _tts_queue.task_done()

_tts_thread = threading.Thread(target=_tts_worker, daemon=True, name="tts-worker")
_tts_thread.start()

def speak(text: str):
    """Thread-safe speak — any thread can call this."""
    _log_line(f"JARVIS: {text}")
    _tts_queue.put(text)

def speak_code_summary(text: str):
    """Strip code fences and speak only the prose explanation."""
    clean = re.sub(r"```[\s\S]*?```", "[code written]", text)
    clean = re.sub(r"`[^`\n]+`", lambda m: m.group().strip("`"), clean)
    speak(clean[:600])

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN + STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def elevate_to_admin():
    if not is_admin():
        params = " ".join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        sys.exit(0)

def set_startup(enable: bool = True) -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY,
                             0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                              f'"{sys.executable}" "{SCRIPT_PATH}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        logging.error(f"Registry: {e}")
        return False

def is_in_startup() -> bool:
    # FIX: only catch FileNotFoundError, not all exceptions
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY)
        winreg.QueryValueEx(k, APP_NAME)
        winreg.CloseKey(k)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False

# ══════════════════════════════════════════════════════════════════════════════
#  MEMORY  —  persistent store for corrections, commands, and learned skills
# ══════════════════════════════════════════════════════════════════════════════

def load_memory() -> dict:
    if os.path.exists(LEARN_FILE):
        try:
            with open(LEARN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "corrections":    [],   # voice corrections ("that was wrong")
        "custom_commands":{},   # user-taught phrase → action mappings
        "learned_skills": {},   # name → {description, steps[], raw_events[]}
    }

def save_memory(m: dict):
    with open(LEARN_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2)

memory = load_memory()

def teach_correction(wrong: str, right: str):
    memory["corrections"].append({
        "wrong": wrong, "right": right,
        "ts": datetime.now().isoformat()
    })
    save_memory(memory)

def teach_command(phrase: str, action_json: dict):
    memory["custom_commands"][phrase.lower()] = action_json
    save_memory(memory)

def get_memory_context() -> str:
    lines = []
    for c in memory["corrections"][-10:]:
        lines.append(f'- Past correction: "{c["wrong"]}" → "{c["right"]}"')
    for p, a in memory["custom_commands"].items():
        lines.append(f'- Custom command: "{p}" → {json.dumps(a)}')
    for name, skill in list(memory["learned_skills"].items())[-8:]:
        desc  = skill.get("description", "no description")
        nstep = len(skill.get("steps", []))
        lines.append(f'- Learned skill "{name}": {desc} ({nstep} steps)')
    return "\n".join(lines) or "No learnings yet."

def get_skill_context() -> str:
    """Full skill details injected when JARVIS needs to execute a learned task."""
    if not memory["learned_skills"]:
        return "No learned skills."
    lines = []
    for name, skill in memory["learned_skills"].items():
        lines.append(f'\nSKILL: "{name}"')
        lines.append(f'  Description: {skill.get("description","")}')
        lines.append(f'  Goal: {skill.get("goal","")}')
        for i, step in enumerate(skill.get("steps", []), 1):
            lines.append(f'  Step {i}: {step}')
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
#  INTELLIGENT RECORDER  —  records raw events then uses AI to UNDERSTAND them
# ══════════════════════════════════════════════════════════════════════════════

_recording        = False
_recorded_events: list = []
_hooks_active     = False
_record_screenshots: list = []   # periodic screenshots during recording

def _mouse_cb(e):
    if _recording and isinstance(e, mouse.ButtonEvent) and e.event_type == "down":
        # Capture a screenshot at each click so AI can see what was clicked
        try:
            import PIL.ImageGrab as IG
            sx, sy = pyautogui.size()
            # Small crop around the click for context
            x, y = int(e.x), int(e.y)
            region = (max(0,x-120), max(0,y-60),
                      min(sx,x+120), min(sy,y+60))
            img  = IG.grab(region)
            path = os.path.join(BASE_DIR, f"_rec_{len(_recorded_events)}.png")
            img.save(path)
            _record_screenshots.append(path)
        except Exception:
            pass
        _recorded_events.append({
            "type": "click", "x": int(e.x), "y": int(e.y),
            "button": e.button,
            "screen_w": pyautogui.size()[0],
            "screen_h": pyautogui.size()[1],
        })

def _key_cb(e):
    if _recording and e.event_type == "down":
        _recorded_events.append({"type": "key", "key": e.name})

def start_recording():
    global _recording, _recorded_events, _hooks_active, _record_screenshots
    _recorded_events    = []
    _record_screenshots = []
    _recording          = True
    if not _hooks_active:
        mouse.hook(_mouse_cb)
        keyboard.hook(_key_cb)
        _hooks_active = True

def stop_recording(name: str) -> list:
    global _recording, _hooks_active
    _recording = False
    if _hooks_active:
        try: mouse.unhook_all()
        except Exception: pass
        try: keyboard.unhook_all()
        except Exception: pass
        _hooks_active = False
    steps = list(_recorded_events)
    # Store raw events immediately so nothing is lost
    if name and steps:
        if "learned_skills" not in memory:
            memory["learned_skills"] = {}
        memory["learned_skills"][name] = {
            "description": f"Recorded {len(steps)} actions",
            "goal":        name,
            "steps":       [f"raw event: {s}" for s in steps],
            "raw_events":  steps,
            "analyzed":    False,
        }
        save_memory(memory)
        # Kick off AI analysis in background — doesn't block the user
        threading.Thread(
            target=_analyze_recording,
            args=(name, steps),
            daemon=True,
            name=f"analyze-{name}"
        ).start()
    return steps

def _analyze_recording(name: str, raw_events: list):
    """
    Send the recorded events to Claude and ask it to:
    1. Understand WHAT the user was trying to do
    2. Convert raw coordinates into logical named steps
    3. Write an adaptive strategy that works in any situation
    """
    _log_line(f"🧠 Analyzing recording '{name}' with AI…")
    speak(f"Analyzing what you did for '{name}'. Give me a moment.")

    # Build a human-readable summary of the events
    event_summary = []
    sw, sh = pyautogui.size()
    for i, ev in enumerate(raw_events):
        if ev["type"] == "click":
            # Convert absolute coords to percentage of screen for portability
            xpct = round(ev["x"] / sw * 100, 1)
            ypct = round(ev["y"] / sh * 100, 1)
            event_summary.append(
                f"  {i+1}. Mouse {ev['button']} click at pixel ({ev['x']},{ev['y']}) "
                f"= {xpct}% across, {ypct}% down the screen"
            )
        elif ev["type"] == "key":
            event_summary.append(f"  {i+1}. Key press: {ev['key']}")

    prompt = f"""The user recorded a sequence of actions on their Windows PC called "{name}".
Here are the raw events:

{chr(10).join(event_summary)}

Screen resolution: {sw}x{sh}

Your job:
1. UNDERSTAND what the user was trying to accomplish (the GOAL, not just the steps).
2. Convert the raw coordinates/keys into LOGICAL named steps (e.g. "Click the Spotify search bar" not "click at 450,300").
3. Write an ADAPTIVE strategy — steps that describe the INTENT so JARVIS can figure out how to do it even if the screen layout changes.
4. Note any steps that look inefficient and suggest the better way.

Respond in this exact JSON format (no markdown):
{{
  "goal": "one sentence describing what this accomplishes",
  "description": "brief description for memory",
  "understood_steps": [
    "Step 1: <logical description of what this step does and why>",
    "Step 2: ...",
    ...
  ],
  "adaptive_strategy": [
    "1. <how JARVIS should accomplish step 1 intelligently, not just by coordinates>",
    ...
  ],
  "efficiency_notes": "any observations about a better way to do this task",
  "jarvis_actions": [
    {{"action": "<action_name>", "args": {{...}}, "speech": "..."}},
    ...
  ]
}}

For jarvis_actions, use only these action names: open_app, open_url, search_web,
play_spotify, play_youtube, type_text, press_key, hotkey, mouse_click,
mouse_move, run_command, screenshot. Make the jarvis_actions the SMART version
of what the user did — use app-level actions (like play_spotify) rather than
raw coordinates wherever possible."""

    try:
        from anthropic import Anthropic as _A
        c   = _A(api_key=ANTHROPIC_API_KEY)
        rsp = c.messages.create(
            model      = "claude-opus-4-5",
            max_tokens = 2048,
            messages   = [{"role": "user", "content": prompt}]
        )
        raw  = rsp.content[0].text.strip()
        clean = re.sub(r"^```(?:json)?\s*", "", raw)
        clean = re.sub(r"\s*```$", "",          clean).strip()
        data  = json.loads(clean)

        # Update the skill with full AI understanding
        memory["learned_skills"][name].update({
            "goal":              data.get("goal", name),
            "description":       data.get("description", name),
            "steps":             data.get("understood_steps", []),
            "adaptive_strategy": data.get("adaptive_strategy", []),
            "efficiency_notes":  data.get("efficiency_notes", ""),
            "jarvis_actions":    data.get("jarvis_actions", []),
            "analyzed":          True,
        })
        save_memory(memory)

        notes = data.get("efficiency_notes", "")
        msg   = (f"I've learned '{name}'. "
                 f"Goal: {data.get('goal','')}. "
                 + (f"Note: {notes}" if notes else ""))
        _log_line(f"✅ Skill '{name}' analyzed successfully.")
        speak(msg[:300])

    except Exception as e:
        logging.error(f"Analysis failed for '{name}': {e}")
        speak(f"I recorded '{name}' but couldn't fully analyze it. "
              "I'll still remember the raw steps.")
    finally:
        # Clean up screenshot crops
        for p in _record_screenshots:
            try: os.unlink(p)
            except: pass

def execute_skill(name: str, context: str = "") -> str:
    """
    Execute a learned skill INTELLIGENTLY.
    If the skill has been AI-analyzed, use the smart jarvis_actions.
    If not yet analyzed, fall back to raw events.
    If the user provides context (e.g. "but open the right playlist"),
    re-plan the steps on the fly.
    """
    skill = memory.get("learned_skills", {}).get(name.lower())
    if not skill:
        # Try fuzzy match
        for k in memory.get("learned_skills", {}):
            if name.lower() in k.lower() or k.lower() in name.lower():
                skill = memory["learned_skills"][k]
                name  = k
                break
    if not skill:
        return f"I haven't learned a skill called '{name}' yet."

    # If user gave extra context or correction, re-plan with AI
    if context.strip():
        return _replan_skill(name, skill, context)

    # Use AI-generated smart actions if available
    if skill.get("analyzed") and skill.get("jarvis_actions"):
        _log_line(f"▶ Executing skill '{name}' using learned strategy.")
        results = []
        for act in skill["jarvis_actions"]:
            r = execute_action(act)
            results.append(r)
            time.sleep(0.4)
        return f"Done. Executed '{name}' using learned strategy."

    # Fall back to raw events
    _log_line(f"▶ Replaying raw events for '{name}' (not yet analyzed).")
    for ev in skill.get("raw_events", []):
        try:
            if ev["type"] == "click":
                pyautogui.click(ev["x"], ev["y"])
            elif ev["type"] == "key":
                pyautogui.press(ev["key"])
        except Exception:
            pass
        time.sleep(0.05)
    return f"Replayed '{name}' (raw mode — analysis still pending)."

def _replan_skill(name: str, skill: dict, correction: str) -> str:
    """
    User said something was wrong. Ask AI to rethink the skill with the correction.
    """
    _log_line(f"🔄 Replanning skill '{name}' with correction: {correction}")
    speak(f"Understood. Rethinking how to {skill.get('goal', name)}.")

    existing = json.dumps({
        "goal":              skill.get("goal",""),
        "steps":             skill.get("steps",[]),
        "adaptive_strategy": skill.get("adaptive_strategy",[]),
        "jarvis_actions":    skill.get("jarvis_actions",[]),
    }, indent=2)

    prompt = f"""JARVIS has a learned skill called "{name}":
{existing}

The user said: "{correction}"

Update this skill. Fix whatever the user identified as wrong.
Think about the most EFFICIENT and RELIABLE way to accomplish the goal.
Respond in the same JSON format as before (no markdown):
{{
  "goal": "...",
  "description": "...",
  "understood_steps": [...],
  "adaptive_strategy": [...],
  "efficiency_notes": "...",
  "jarvis_actions": [...]
}}"""

    try:
        from anthropic import Anthropic as _A
        c   = _A(api_key=ANTHROPIC_API_KEY)
        rsp = c.messages.create(
            model="claude-opus-4-5", max_tokens=2048,
            messages=[{"role":"user","content":prompt}]
        )
        raw   = rsp.content[0].text.strip()
        clean = re.sub(r"^```(?:json)?\s*","",raw)
        clean = re.sub(r"\s*```$","",clean).strip()
        data  = json.loads(clean)
        memory["learned_skills"][name].update({
            "goal":              data.get("goal",""),
            "description":       data.get("description",""),
            "steps":             data.get("understood_steps",[]),
            "adaptive_strategy": data.get("adaptive_strategy",[]),
            "efficiency_notes":  data.get("efficiency_notes",""),
            "jarvis_actions":    data.get("jarvis_actions",[]),
            "analyzed":          True,
        })
        save_memory(memory)
        speak(f"Updated. I now know a better way to {skill.get('goal',name)}.")
        return f"Skill '{name}' updated with your correction."
    except Exception as e:
        logging.error(f"Replan failed: {e}")
        return f"Couldn't update skill: {e}"

# ══════════════════════════════════════════════════════════════════════════════
#  MICROPHONE
# ══════════════════════════════════════════════════════════════════════════════

def listen_once(timeout: int = 8, phrase_limit: int = 15) -> str | None:
    r = sr.Recognizer()
    r.dynamic_energy_threshold = True
    with sr.Microphone() as src:
        r.adjust_for_ambient_noise(src, duration=0.3)
        try:
            audio = r.listen(src, timeout=timeout, phrase_time_limit=phrase_limit)
            return r.recognize_google(audio).lower()
        except Exception:
            return None

# ══════════════════════════════════════════════════════════════════════════════
#  FILE SEARCH
# ══════════════════════════════════════════════════════════════════════════════

SEARCH_ROOTS = [
    os.path.expanduser("~"),
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\Users",
]

def find_file(name: str, max_results: int = 5) -> list:
    results = []
    pattern = f"*{name}*" if "*" not in name else name
    for root in SEARCH_ROOTS:
        if not os.path.exists(root):
            continue
        try:
            for p in glob.glob(os.path.join(root, "**", pattern), recursive=True):
                results.append(p)
                if len(results) >= max_results:
                    return results
        except Exception:
            pass
    return results

# ══════════════════════════════════════════════════════════════════════════════
#  TYPING HELPER
#  FIX: pyautogui.write() silently drops Unicode chars (emoji, accents, etc.)
#       Use clipboard paste when pyperclip is available.
# ══════════════════════════════════════════════════════════════════════════════

def safe_type(text: str):
    if HAS_PYPERCLIP:
        try:
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            return
        except Exception:
            pass
    pyautogui.write(text, interval=0.03)

# ══════════════════════════════════════════════════════════════════════════════
#  SCREEN AWARENESS ENGINE
#  Captures the current screen and sends it to Claude Vision so JARVIS
#  understands what app is open, what buttons are visible, what's happening.
# ══════════════════════════════════════════════════════════════════════════════

import base64
from io import BytesIO

def capture_screen_base64(region=None, scale=0.5) -> str:
    """
    Take a screenshot, scale it down for faster API calls,
    and return as base64-encoded PNG string.
    scale=0.5 means half resolution — good balance of detail vs speed.
    """
    try:
        from PIL import Image
        img = pyautogui.screenshot(region=region)
        # Scale down to reduce tokens/cost
        w, h = img.size
        img  = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf  = BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logging.error(f"Screen capture failed: {e}")
        return ""

def describe_screen() -> str:
    """
    Ask Claude to describe what is currently on screen.
    Returns a plain text description JARVIS can use to plan actions.
    """
    b64 = capture_screen_base64()
    if not b64:
        return "Could not capture screen."
    try:
        client = get_client()
        resp   = client.messages.create(
            model      = "claude-opus-4-5",
            max_tokens = 512,
            messages   = [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": "image/png",
                            "data":       b64,
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe what is on this screen briefly and practically. "
                            "Focus on: what app is open, what is visible/clickable, "
                            "any error messages, and the current state. "
                            "Be concise — 2-3 sentences max."
                        )
                    }
                ]
            }]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logging.error(f"Screen describe failed: {e}")
        return f"Screen description failed: {e}"

def screen_aware_ask(user_command: str) -> str:
    """
    Send user command PLUS a screenshot to Claude so it can
    give context-aware instructions based on what's actually on screen.
    """
    b64 = capture_screen_base64()
    if not b64:
        return ask_jarvis(user_command)   # fallback to text-only

    system = SYSTEM_TMPL.format(
        memory=get_memory_context(),
        skills=get_skill_context()
    )
    try:
        client = get_client()
        resp   = client.messages.create(
            model      = "claude-opus-4-5",
            max_tokens = 1024,
            system     = system,
            messages   = _history + [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": "image/png",
                            "data":       b64,
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            f"This is my current screen. "
                            f"My request: {user_command}\n"
                            f"Use what you see on screen to give the most accurate response or action."
                        )
                    }
                ]
            }]
        )
        reply = resp.content[0].text.strip()
        _history.append({"role": "user",      "content": user_command})
        _history.append({"role": "assistant",  "content": reply})

        # Parse JSON action same as ask_jarvis
        clean = re.sub(r"^```(?:json)?\s*", "", reply)
        clean = re.sub(r"\s*```$",           "", clean).strip()
        if clean.startswith("{"):
            try:
                data   = json.loads(clean)
                speech = data.get("speech", "Done.")
                result = execute_action(data)
                return result if data.get("action") in INFO_ACTIONS else speech
            except json.JSONDecodeError:
                pass
        elif clean.startswith("["):
            try:
                steps = json.loads(clean)
                last  = "Done."
                for step in steps:
                    r    = execute_action(step)
                    last = step.get("speech", r)
                    time.sleep(0.3)
                return last
            except json.JSONDecodeError:
                pass
        return reply
    except Exception as e:
        logging.error(f"Screen-aware ask failed: {e}")
        return ask_jarvis(user_command)

# ══════════════════════════════════════════════════════════════════════════════
#  WEB BROWSING ENGINE
#  Fetch live web content so JARVIS can answer questions about current events,
#  weather, news, sports scores, prices — anything on the internet.
# ══════════════════════════════════════════════════════════════════════════════

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Free APIs — no key needed
WEATHER_API   = "https://wttr.in/{city}?format=3"
NEWS_API      = "https://feeds.bbcnews.com/news/rss.xml"
SEARCH_DDG    = "https://html.duckduckgo.com/html/?q={query}"

def web_fetch(url: str, timeout: int = 8) -> str:
    """Fetch raw text content from a URL."""
    if not HAS_REQUESTS:
        return "requests library not installed. Run: pip install requests"
    try:
        headers = {"User-Agent": "Mozilla/5.0 JARVIS/3.0"}
        r = _requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text[:8000]   # cap to avoid huge responses
    except Exception as e:
        return f"Fetch failed: {e}"

def web_search_and_summarize(query: str) -> str:
    """
    Search DuckDuckGo and ask Claude to summarize the results.
    No API key needed — uses the free HTML interface.
    """
    url  = SEARCH_DDG.format(query=urllib.parse.quote(query))
    html = web_fetch(url)
    if "Fetch failed" in html:
        return html

    # Strip HTML tags for cleaner text
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+",     " ", clean).strip()[:3000]

    try:
        client = get_client()
        resp   = client.messages.create(
            model      = "claude-opus-4-5",
            max_tokens = 256,
            messages   = [{
                "role": "user",
                "content": (
                    f'Search results for "{query}":\n\n{clean}\n\n'
                    f"Summarize the most relevant answer in 2-3 sentences, "
                    f"spoken naturally as JARVIS would say it."
                )
            }]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        return f"Could not summarize: {e}"

def get_weather(city: str) -> str:
    """Get current weather for a city — no key needed (wttr.in)."""
    url  = WEATHER_API.format(city=urllib.parse.quote(city))
    data = web_fetch(url, timeout=5)
    if "Fetch failed" in data:
        return f"Could not get weather for {city}."
    return data.strip()

def fetch_news() -> str:
    """Get BBC news headlines — no key needed."""
    xml = web_fetch(NEWS_API, timeout=6)
    if "Fetch failed" in xml:
        return "Could not fetch news."
    # Extract titles from RSS
    titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml)
    if not titles:
        titles = re.findall(r"<title>(.*?)</title>", xml)
    headlines = [t for t in titles if t and "BBC" not in t][:5]
    if not headlines:
        return "No headlines found."
    return "Top headlines: " + ". ".join(headlines) + "."


#  Uses Stable Diffusion locally (no key needed) via diffusers.
#  Falls back to a simple placeholder if diffusers not installed.
#  Auto-saves as PNG to Desktop and opens in Windows Photos app.
# ══════════════════════════════════════════════════════════════════════════════

IMAGE_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "JARVIS_Images")
os.makedirs(IMAGE_DIR, exist_ok=True)

# Check if diffusers/torch available for local Stable Diffusion
try:
    from diffusers import StableDiffusionPipeline
    import torch
    HAS_DIFFUSERS = True
except ImportError:
    HAS_DIFFUSERS = False

_sd_pipeline = None   # lazy-loaded so startup isn't slow

def _load_sd_pipeline():
    """Load Stable Diffusion pipeline once, reuse after."""
    global _sd_pipeline
    if _sd_pipeline is not None:
        return _sd_pipeline
    _log_line("Loading Stable Diffusion model (first time may take a minute)…")
    speak("Loading image model. One moment sir.")
    try:
        # Use float32 for CPU compatibility, float16 for GPU
        device    = "cuda" if torch.cuda.is_available() else "cpu"
        dtype     = torch.float16 if device == "cuda" else torch.float32
        _sd_pipeline = StableDiffusionPipeline.from_pretrained(
            "runwayml/stable-diffusion-v1-5",
            torch_dtype=dtype,
            safety_checker=None,         # remove NSFW filter for speed
            requires_safety_checker=False
        ).to(device)
        if device == "cpu":
            # CPU optimisation — reduce memory usage
            _sd_pipeline.enable_attention_slicing()
        _log_line(f"Stable Diffusion loaded on {device.upper()}.")
        return _sd_pipeline
    except Exception as e:
        logging.error(f"SD load failed: {e}")
        return None

def generate_image(prompt: str, filename: str = "") -> str:
    """
    Generate an image from a text prompt.
    Saves as PNG to Desktop/JARVIS_Images/ and opens in Photos app.
    Returns the file path or an error message.
    """
    if not filename:
        # Auto-name from prompt — sanitise for filesystem
        safe = re.sub(r"[^\w\s-]", "", prompt)[:40].strip().replace(" ", "_")
        filename = f"{safe}_{int(time.time())}.png"
    if not filename.endswith(".png"):
        filename += ".png"

    save_path = os.path.join(IMAGE_DIR, filename)

    if HAS_DIFFUSERS:
        # ── Local Stable Diffusion ──────────────────────────────────────────
        pipe = _load_sd_pipeline()
        if pipe is None:
            return _image_fallback(prompt, save_path)
        try:
            _log_line(f"Generating image: '{prompt}'")
            speak("Generating your image now.")
            result = pipe(
                prompt,
                num_inference_steps=25,   # balance speed vs quality
                guidance_scale=7.5,
                width=512,
                height=512,
            )
            img = result.images[0]
            img.save(save_path, "PNG")
            _log_line(f"Image saved: {save_path}")
            _open_in_photos(save_path)
            return save_path
        except Exception as e:
            logging.error(f"Image generation failed: {e}")
            return _image_fallback(prompt, save_path)
    else:
        # ── Fallback: generate a simple placeholder PNG with PIL ────────────
        return _image_fallback(prompt, save_path)

def _image_fallback(prompt: str, save_path: str) -> str:
    """
    If Stable Diffusion isn't installed, create a labelled placeholder PNG
    and tell the user how to install the real thing.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        # Dark gradient background
        img  = Image.new("RGB", (512, 512), color=(15, 20, 30))
        draw = ImageDraw.Draw(img)

        # Border
        draw.rectangle([4, 4, 507, 507], outline=(0, 207, 255), width=2)

        # Title
        draw.text((20, 20),  "J.A.R.V.I.S",      fill=(0, 207, 255))
        draw.text((20, 50),  "Image Generator",    fill=(0, 207, 255))
        draw.text((20, 90),  "PROMPT:",            fill=(120, 180, 200))

        # Word-wrap the prompt
        words, line, y = prompt.split(), "", 120
        for word in words:
            test = (line + " " + word).strip()
            if len(test) > 40:
                draw.text((20, y), line, fill=(200, 220, 240))
                line = word; y += 24
            else:
                line = test
        if line:
            draw.text((20, y), line, fill=(200, 220, 240))

        draw.text((20, 440), "Install diffusers for real images:", fill=(255, 170, 0))
        draw.text((20, 464), "pip install diffusers transformers", fill=(255, 200, 100))
        draw.text((20, 488), "pip install torch accelerate",       fill=(255, 200, 100))

        img.save(save_path, "PNG")
        _open_in_photos(save_path)
        return save_path
    except Exception as e:
        return f"Image generation failed: {e}"

def _open_in_photos(path: str):
    """Open a PNG file in the Windows Photos app automatically."""
    try:
        # Windows Photos app URI scheme
        subprocess.Popen(
            f'explorer.exe "{path}"',
            shell=True
        )
        _log_line(f"Opened in Photos: {path}")
    except Exception as e:
        logging.error(f"Could not open Photos: {e}")

_last_code   = ""
_last_lang   = "python"
_code_output = ""
_code_tab_ref = None   # set when CodeTab is built

LANG_RUNNERS = {
    "python":     ("python",                              ".py"),
    "javascript": ("node",                                ".js"),
    "js":         ("node",                                ".js"),
    "bash":       ("bash",                                ".sh"),
    "powershell": ("powershell -ExecutionPolicy Bypass -File", ".ps1"),
    "batch":      ("cmd /c",                              ".bat"),
    "c":          (None,                                  ".c"),
    "cpp":        (None,                                  ".cpp"),
    "go":         ("go run",                              ".go"),
    "ruby":       ("ruby",                                ".rb"),
    "rust":       (None,                                  ".rs"),
}

# FIX: correct Notepad++ executable path on Windows
EDITORS = [
    "code",                                                       # VS Code
    r"C:\Program Files\Notepad++\notepad++.exe",
    r"C:\Program Files (x86)\Notepad++\notepad++.exe",
    "notepad",                                                    # always present
]

def _detect_lang(code: str, hint: str = "") -> str:
    hint = hint.lower()
    for lang in LANG_RUNNERS:
        if lang in hint:
            return lang
    if re.search(r"\bdef \w+\(|^\s*import \w+|print\(", code, re.M): return "python"
    if re.search(r"function\s*\w*\s*\(|const |let |var ", code):     return "javascript"
    if re.search(r"#include\s*<.*>",                         code):   return "c"
    if re.search(r"package main|func main\(\)",              code):   return "go"
    return "python"

def run_code(code: str, lang: str = "python", timeout: int = 15) -> str:
    global _code_output
    lang            = _detect_lang(code, lang)
    runner, ext     = LANG_RUNNERS.get(lang, ("python", ".py"))
    exe_to_delete   = None

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, mode="w", encoding="utf-8")
    tmp.write(code); tmp.close()
    src_path = tmp.name

    try:
        if lang in ("c", "cpp", "rust"):
            out_exe     = src_path.replace(ext, ".exe")
            exe_to_delete = out_exe
            if lang == "c":
                comp_cmd = f'gcc "{src_path}" -o "{out_exe}"'
            elif lang == "cpp":
                comp_cmd = f'g++ "{src_path}" -o "{out_exe}"'
            else:  # rust
                comp_cmd = f'rustc "{src_path}" -o "{out_exe}"'
            comp = subprocess.run(comp_cmd, shell=True,
                                  capture_output=True, text=True, timeout=60)
            if comp.returncode != 0:
                _code_output = comp.stderr.strip()
                return f"Compile error:\n{_code_output}"
            result = subprocess.run(f'"{out_exe}"', shell=True,
                                    capture_output=True, text=True, timeout=timeout)
        else:
            result = subprocess.run(
                f'{runner} "{src_path}"', shell=True,
                capture_output=True, text=True, timeout=timeout
            )

        out = result.stdout.strip()
        err = result.stderr.strip()
        _code_output = (out + ("\n" + err if err else "")).strip() or "(no output)"
        return _code_output

    except subprocess.TimeoutExpired:
        _code_output = "Execution timed out."
        return _code_output
    except Exception as e:
        _code_output = f"Run error: {e}"
        return _code_output
    finally:
        # FIX: clean up both source and compiled exe
        for f in [src_path, exe_to_delete]:
            if f:
                try: os.unlink(f)
                except Exception: pass

def save_code_to_file(code: str, filename: str, lang: str = "python") -> str:
    _, ext = LANG_RUNNERS.get(lang, ("", ".py"))
    if not filename.endswith(ext):
        filename += ext
    path = os.path.join(CODE_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return path

def open_code_in_editor(path: str) -> str:
    # FIX: use correct editor paths, not bare "notepad++"
    for editor in EDITORS:
        try:
            subprocess.Popen(f'"{editor}" "{path}"', shell=True)
            name = os.path.basename(editor).replace(".exe", "")
            return f"Opened in {name}."
        except Exception:
            pass
    try:
        os.startfile(path)
        return "Opened in default editor."
    except Exception as e:
        return f"Could not open editor: {e}"

# ══════════════════════════════════════════════════════════════════════════════
#  ANTHROPIC CLIENT — lazy init to avoid crash if key not set at import time
# ══════════════════════════════════════════════════════════════════════════════

_client: Anthropic | None = None

def get_client() -> Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. "
                "Run: setx ANTHROPIC_API_KEY \"sk-ant-...\""
            )
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client

# ══════════════════════════════════════════════════════════════════════════════
#  ACTION EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════

def execute_action(act_dict: dict) -> str:
    global _last_code, _last_lang
    act  = act_dict.get("action", "").lower()
    args = act_dict.get("args", {})

    try:
        # ── Apps ──────────────────────────────────────────────────────────────
        if act == "open_app":
            name = args.get("name", "")
            subprocess.Popen(name, shell=True)
            return f"Opening {name}."

        # ── Files ─────────────────────────────────────────────────────────────
        elif act == "find_file":
            results = find_file(args.get("name", ""))
            return ("Found: " + "; ".join(results[:3])) if results else "No file found."

        elif act == "open_file":
            path = args.get("path", "")
            if not path:
                results = find_file(args.get("name", ""))
                path    = results[0] if results else ""
            if path and os.path.exists(path):
                os.startfile(path)
                return f"Opened {os.path.basename(path)}."
            return "File not found."

        # ── Browser ───────────────────────────────────────────────────────────
        elif act == "open_url":
            webbrowser.open(args.get("url", ""))
            return "Browser opened."

        elif act == "search_web":
            q = urllib.parse.quote(args.get("query", ""))
            webbrowser.open(f"https://www.google.com/search?q={q}")
            return "Searching."

        elif act == "search_youtube":
            q = urllib.parse.quote(args.get("query", ""))
            webbrowser.open(f"https://www.youtube.com/results?search_query={q}")
            return "Searching YouTube."

        elif act == "play_spotify":
            query = args.get("query", "")
            spotify_paths = [
                os.path.join(os.environ.get("APPDATA",""),      "Spotify", "Spotify.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA",""), "Spotify", "Spotify.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA",""), "Microsoft", "WindowsApps", "Spotify.exe"),
            ]
            opened = False
            for sp in spotify_paths:
                if os.path.exists(sp):
                    subprocess.Popen(f'"{sp}"', shell=True)
                    opened = True
                    time.sleep(3)
                    break
            if not opened:
                # Fallback: web player search
                webbrowser.open(f"https://open.spotify.com/search/{urllib.parse.quote(query)}")
                return f"Opened Spotify web search for {query}."
            # Focus search with Ctrl+L, type query, hit enter
            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.5)
            safe_type(query)
            time.sleep(0.4)
            pyautogui.press("enter")
            time.sleep(1.5)
            pyautogui.press("enter")   # play first result
            return f"Playing {query} on Spotify."

        elif act == "play_youtube":
            query = args.get("query", "")
            webbrowser.open(f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}")
            time.sleep(3)
            # Tab to first video result and press enter
            pyautogui.hotkey("alt", "d"); time.sleep(0.3)
            for _ in range(6):
                pyautogui.press("tab"); time.sleep(0.15)
            pyautogui.press("enter")
            return f"Playing {query} on YouTube."

        # ── Keyboard ──────────────────────────────────────────────────────────
        elif act == "type_text":
            safe_type(args.get("text", ""))
            return "Typed."

        elif act == "press_key":
            pyautogui.press(args.get("key", ""))
            return f"Pressed {args.get('key')}."

        elif act == "hotkey":
            pyautogui.hotkey(*args.get("keys", []))
            return "Hotkey sent."

        elif act == "hold_key":
            k = args.get("key", "")
            d = float(args.get("duration", 1))
            pyautogui.keyDown(k); time.sleep(d); pyautogui.keyUp(k)
            return f"Held {k}."

        # ── Mouse ─────────────────────────────────────────────────────────────
        elif act == "mouse_move":
            pyautogui.moveTo(args["x"], args["y"],
                             duration=float(args.get("duration", 0.3)))
            return "Mouse moved."

        elif act == "mouse_click":
            btn = args.get("button", "left")
            x, y = args.get("x"), args.get("y")
            if x is not None:
                pyautogui.click(x, y, button=btn)
            else:
                pyautogui.click(button=btn)
            return f"{btn.capitalize()} click."

        elif act == "mouse_double_click":
            x, y = args.get("x"), args.get("y")
            if x is not None:
                pyautogui.doubleClick(x, y)
            else:
                pyautogui.doubleClick()
            return "Double-clicked."

        elif act == "mouse_drag":
            pyautogui.dragTo(args["x2"], args["y2"],
                             duration=float(args.get("duration", 0.5)),
                             button="left")
            return "Dragged."

        elif act == "mouse_scroll":
            pyautogui.scroll(int(args.get("clicks", 3)))
            return "Scrolled."

        elif act == "get_mouse_pos":
            x, y = pyautogui.position()
            return f"Mouse at ({x}, {y})."

        elif act == "screenshot":
            p = os.path.join(os.path.expanduser("~"), "Desktop",
                             f"jarvis_{int(time.time())}.png")
            pyautogui.screenshot(p)
            return "Screenshot saved to Desktop."

        # ── System ────────────────────────────────────────────────────────────
        elif act == "get_system_info":
            cpu  = psutil.cpu_percent(interval=1)
            ram  = psutil.virtual_memory()
            disk = psutil.disk_usage("C:\\")
            return (f"CPU {cpu}%, RAM {ram.percent}% used "
                    f"({ram.available // (1024**3)} GB free), "
                    f"Disk {disk.percent}% used.")

        elif act == "get_time":
            n = datetime.now()
            return f"It is {n.strftime('%I:%M %p')}, {n.strftime('%A %B %d %Y')}."

        elif act == "run_command":
            r = subprocess.run(args.get("command", ""), shell=True,
                               capture_output=True, text=True, timeout=20)
            return (r.stdout.strip() or r.stderr.strip() or "Done.")[:500]

        elif act == "volume_up":
            for _ in range(int(args.get("steps", 5))): pyautogui.press("volumeup")
            return "Volume raised."

        elif act == "volume_down":
            for _ in range(int(args.get("steps", 5))): pyautogui.press("volumedown")
            return "Volume lowered."

        elif act == "volume_mute":
            pyautogui.press("volumemute")
            return "Mute toggled."

        # ── Recorder ──────────────────────────────────────────────────────────
        elif act == "start_recording":
            start_recording()
            return "Recording your actions. Say 'Jarvis stop recording' when done."

        elif act == "stop_recording":
            name  = args.get("name", "unnamed")
            steps = stop_recording(name)
            return (f"Got it. Recorded {len(steps)} actions for '{name}'. "
                    "Analyzing now in the background.")

        elif act == "execute_skill":
            name    = args.get("name", "")
            context = args.get("context", "")
            return execute_skill(name, context)

        elif act == "list_skills":
            skills = list(memory.get("learned_skills", {}).keys())
            if not skills:
                return "No learned skills yet. Show me something to do and I'll learn it."
            return "I know how to: " + ", ".join(skills) + "."

        elif act == "correct_skill":
            name       = args.get("name", "")
            correction = args.get("correction", "")
            return _replan_skill(name,
                                 memory.get("learned_skills",{}).get(name,{}),
                                 correction)

        elif act == "teach_correction":
            teach_correction(args.get("wrong", ""), args.get("right", ""))
            return "Correction noted."

        elif act == "teach_command":
            teach_command(args.get("phrase", ""), args.get("action_obj", {}))
            return "Custom command saved."

        # ── Startup ───────────────────────────────────────────────────────────
        elif act == "enable_startup":
            return "Startup enabled." if set_startup(True) else "Startup failed."

        elif act == "disable_startup":
            set_startup(False)
            return "Startup disabled."

        # ── CODING ────────────────────────────────────────────────────────────

        elif act == "write_code":
            code = args.get("code", "")
            lang = args.get("lang", "python")
            _last_code = code
            _last_lang = lang
            # FIX: guard against _code_tab_ref being None (race condition)
            if _code_tab_ref is not None:
                _code_tab_ref.set_code(code, lang)
            return (f"Code written in {lang}. "
                    "Say 'run it', 'save it', or 'open it in editor'.")

        elif act == "run_last_code":
            if not _last_code:
                return "No code to run yet."
            out   = run_code(_last_code, _last_lang)
            if _code_tab_ref is not None:
                _code_tab_ref.set_output(out)
            lines = out.strip().split("\n")
            return f"Done. Output: {'; '.join(lines[:3])[:200]}"

        elif act == "run_code_snippet":
            code = args.get("code", "")
            lang = args.get("lang", "python")
            _last_code = code
            _last_lang = lang
            if _code_tab_ref is not None:
                _code_tab_ref.set_code(code, lang)
            out = run_code(code, lang)
            if _code_tab_ref is not None:
                _code_tab_ref.set_output(out)
            lines = out.strip().split("\n")
            return f"Done. Output: {'; '.join(lines[:3])[:200]}"

        elif act == "save_code":
            if not _last_code:
                return "No code to save."
            name = args.get("filename", f"jarvis_{int(time.time())}")
            path = save_code_to_file(_last_code, name, _last_lang)
            return f"Saved to {path}."

        elif act == "open_code_editor":
            if not _last_code:
                return "No code to open."
            name = args.get("filename", f"jarvis_edit_{int(time.time())}")
            path = save_code_to_file(_last_code, name, _last_lang)
            return open_code_in_editor(path)

        elif act == "install_package":
            pkg = args.get("package", "")
            r   = subprocess.run(f'pip install {pkg}', shell=True,
                                 capture_output=True, text=True, timeout=120)
            out = r.stdout.strip() or r.stderr.strip()
            return f"pip install {pkg}: " + (out[:300] or "done.")

        # ── SCREEN AWARENESS ──────────────────────────────────────────────────
        elif act == "describe_screen":
            desc = describe_screen()
            return desc

        elif act == "screen_click":
            # JARVIS looks at screen then finds and clicks what user described
            target = args.get("target", "")
            desc   = describe_screen()
            # Ask AI where to click based on screen description
            client = get_client()
            r2 = client.messages.create(
                model="claude-opus-4-5", max_tokens=128,
                messages=[{"role":"user","content":(
                    f"Screen contents: {desc}\n"
                    f"User wants to click: {target}\n"
                    f"Reply with ONLY a JSON: {{\"x\": <pixel_x>, \"y\": <pixel_y>}} "
                    f"or {{\"not_found\": true}} if not visible."
                )}]
            )
            try:
                coords = json.loads(r2.content[0].text.strip())
                if coords.get("not_found"):
                    return f"Could not find '{target}' on screen."
                pyautogui.click(coords["x"], coords["y"])
                return f"Clicked '{target}'."
            except Exception:
                return f"Could not locate '{target}' on screen."

        # ── WEB BROWSING ──────────────────────────────────────────────────────
        elif act == "web_search":
            query = args.get("query", "")
            return web_search_and_summarize(query)

        elif act == "get_weather":
            city = args.get("city", "")
            return get_weather(city)

        elif act == "get_news":
            return fetch_news()

        elif act == "web_fetch_url":
            url  = args.get("url", "")
            html = web_fetch(url)
            # Summarise the fetched page
            clean = re.sub(r"<[^>]+>", " ", html)
            clean = re.sub(r"\s+",     " ", clean).strip()[:3000]
            try:
                client = get_client()
                r2 = client.messages.create(
                    model="claude-opus-4-5", max_tokens=256,
                    messages=[{"role":"user","content":(
                        f"Page content from {url}:\n{clean}\n\n"
                        f"Summarize in 2-3 sentences as JARVIS would say it."
                    )}]
                )
                return r2.content[0].text.strip()
            except Exception as e:
                return clean[:300]

        # ── IMAGE GENERATION ──────────────────────────────────────────────────
        elif act == "generate_image":
            prompt   = args.get("prompt", "")
            filename = args.get("filename", "")
            if not prompt:
                return "No prompt provided for image generation."
            path = generate_image(prompt, filename)
            if os.path.exists(path):
                name = os.path.basename(path)
                return (f"Image saved as '{name}' on your Desktop in JARVIS_Images. "
                        "Opening in Photos now.")
            return path   # error message

        # ── Exit ──────────────────────────────────────────────────────────────
        elif act == "shutdown_jarvis":
            speak("Goodbye, sir.")
            time.sleep(1.5)
            os._exit(0)

        else:
            return f"Unknown action: {act}"

    except Exception as e:
        logging.error(f"Action '{act}' error: {e}")
        return f"Action failed: {e}"

# ══════════════════════════════════════════════════════════════════════════════
#  AI BRAIN
# ══════════════════════════════════════════════════════════════════════════════

_history: list = []

SYSTEM_TMPL = """\
You are JARVIS, an advanced AI assistant with FULL control over a Windows PC \
and expert-level coding ability in all languages. Be concise, confident. \
Occasionally address the user as "sir".

LEARNED MEMORY:
{memory}

LEARNED SKILLS (things I have been shown how to do):
{skills}

════════════════════════════════════════════════════
RESPONSE FORMAT
════════════════════════════════════════════════════
• Computer/code ACTION → raw JSON ONLY (no markdown fences, no surrounding text):
  {{"action":"<name>","args":{{...}},"speech":"spoken summary"}}

• MULTI-STEP → raw JSON array of action objects.

• CONVERSATION / explanation → plain text only.

════════════════════════════════════════════════════
CODING RULES
════════════════════════════════════════════════════
WRITE code         → action "write_code",       args {{"code":"...","lang":"python"}}
WRITE + RUN code   → action "run_code_snippet",  args {{"code":"...","lang":"python"}}
RUN existing code  → action "run_last_code",     args {{}}
SAVE code          → action "save_code",         args {{"filename":"name"}}
OPEN in editor     → action "open_code_editor",  args {{"filename":"name"}}
INSTALL package    → action "install_package",   args {{"package":"requests"}}
EXPLAIN / DEBUG    → plain text (no JSON)

Always return COMPLETE, working code — never truncate or use placeholders.

════════════════════════════════════════════════════
IMAGE GENERATION
════════════════════════════════════════════════════
When user asks to generate, create, draw, or make an image/picture/photo:
  → action "generate_image", args {{"prompt":"detailed description","filename":"optional_name"}}
  The prompt should be detailed and descriptive for best results.
  Example: "generate_image" with prompt "a futuristic city at night with neon lights and flying cars"
  The image auto-saves as PNG to Desktop and opens in Windows Photos automatically.

════════════════════════════════════════════════════
ALL ACTIONS
════════════════════════════════════════════════════
open_app            {{"name":"notepad"}}
find_file           {{"name":"resume.pdf"}}
open_file           {{"name":"report.docx"}} | {{"path":"C:\\\\...\\\\file"}}
open_url            {{"url":"https://..."}}
search_web          {{"query":"..."}}
search_youtube      {{"query":"..."}}
play_spotify        {{"query":"song or artist name"}}
play_youtube        {{"query":"song or video name"}}
type_text           {{"text":"Hello"}}
press_key           {{"key":"enter"}}
hotkey              {{"keys":["ctrl","c"]}}
hold_key            {{"key":"shift","duration":1}}
mouse_move          {{"x":500,"y":300}}
mouse_click         {{"x":500,"y":300,"button":"left"}}
mouse_double_click  {{"x":500,"y":300}}
mouse_drag          {{"x2":700,"y2":400}}
mouse_scroll        {{"clicks":3}}
get_mouse_pos       {{}}
screenshot          {{}}
get_system_info     {{}}
get_time            {{}}
run_command         {{"command":"dir C:\\\\"}}
volume_up           {{"steps":5}}
volume_down         {{"steps":5}}
volume_mute         {{}}
LEARNING:
start_recording     {{}}   ← when user says "watch me", "learn this", "start recording"
stop_recording      {{"name":"open spotify"}}   ← when user says "stop", "done", "save that as X"
execute_skill       {{"name":"open spotify","context":""}}   ← when user says "do that thing", "open spotify like I showed you"
correct_skill       {{"name":"open spotify","correction":"you opened the wrong playlist"}}   ← when user says a skill was wrong
list_skills         {{}}   ← when user asks what you know how to do

IMPORTANT CORRECTION HANDLING:
- If user says "[skill name] was wrong" or "you did [skill] wrong" or "that's not how to [skill]"
  → use correct_skill action with their explanation as the correction field
- If user says "no" or "wrong" after you do something
  → ask "What should I have done instead?" then use correct_skill
- Skills improve every time the user corrects them — never just repeat the old way
teach_correction    {{"wrong":"old","right":"correct"}}
teach_command       {{"phrase":"open my report","action_obj":{{"action":"open_file","args":{{"name":"report.docx"}}}}}}
enable_startup      {{}}
disable_startup     {{}}
write_code          {{"code":"...","lang":"python"}}
run_code_snippet    {{"code":"...","lang":"python"}}
run_last_code       {{}}
save_code           {{"filename":"my_script"}}
open_code_editor    {{"filename":"my_script"}}
generate_image      {{"prompt":"a dog on the moon","filename":"my_image"}}
install_package     {{"package":"requests"}}
describe_screen     {{}}
screen_click        {{"target":"the save button"}}
web_search          {{"query":"current bitcoin price"}}
get_weather         {{"city":"London"}}
get_news            {{}}
web_fetch_url       {{"url":"https://example.com"}}
shutdown_jarvis     {{}}

════════════════════════════════════════════════════
SCREEN & WEB RULES
════════════════════════════════════════════════════
- User says "click X" / "find X" on screen → screen_click
- User asks about screen contents → describe_screen
- User asks weather → get_weather
- User asks news / headlines → get_news
- User asks about anything current / real-time → web_search
- User gives a URL to read → web_fetch_url
- Always use screen context for vague spatial commands like "click that" or "hit ok"
"""

INFO_ACTIONS = {
    "get_system_info", "get_time", "run_command", "find_file",
    "get_mouse_pos", "stop_recording", "execute_skill", "list_skills",
    "run_last_code", "run_code_snippet", "save_code",
    "open_code_editor", "install_package", "write_code", "generate_image",
    "describe_screen", "screen_click", "web_search", "get_weather",
    "get_news", "web_fetch_url",
}

def ask_jarvis(user_input: str) -> str:
    # Custom voice commands take priority
    for phrase, act in memory["custom_commands"].items():
        if phrase in user_input.lower():
            return execute_action(act)

    try:
        client = get_client()
    except RuntimeError as e:
        return str(e)

    system = SYSTEM_TMPL.format(
        memory=get_memory_context(),
        skills=get_skill_context()
    )
    _history.append({"role": "user", "content": user_input})

    try:
        resp  = client.messages.create(
            model      = "claude-opus-4-5",
            max_tokens = 4096,
            system     = system,
            messages   = _history,
        )
        reply = resp.content[0].text.strip()
    except Exception as e:
        logging.error(f"API: {e}")
        return f"API error: {e}"

    _history.append({"role": "assistant", "content": reply})

    # Strip accidental markdown fences before JSON parse
    clean = re.sub(r"^```(?:json)?\s*", "", reply.strip())
    clean = re.sub(r"\s*```$",          "", clean).strip()

    if clean.startswith("{"):
        try:
            data   = json.loads(clean)
            speech = data.get("speech", "Done.")
            result = execute_action(data)
            return result if data.get("action") in INFO_ACTIONS else speech
        except json.JSONDecodeError:
            pass
    elif clean.startswith("["):
        try:
            steps = json.loads(clean)
            last  = "Done."
            for step in steps:
                r    = execute_action(step)
                last = step.get("speech", r)
                time.sleep(0.3)
            return last
        except json.JSONDecodeError:
            pass

    return reply   # plain conversational answer

# ══════════════════════════════════════════════════════════════════════════════
#  GUI  —  colour tokens
# ══════════════════════════════════════════════════════════════════════════════

BG     = "#090d12"
BG2    = "#0d1520"
BG3    = "#111c2a"
ACCENT = "#00cfff"
GREEN  = "#00ff88"
RED    = "#ff4466"
AMBER  = "#ffaa00"
PURPLE = "#bb88ff"
FG     = "#c8d8e4"
FG2    = "#778899"

_status_var: tk.StringVar | None = None

def _mk_btn(parent, label, cmd, fg=ACCENT, bg=BG2):
    b = tk.Button(parent, text=label, command=cmd,
                  bg=bg, fg=fg, activebackground=BG3, activeforeground=fg,
                  relief="flat", bd=0, font=("Courier New", 9, "bold"),
                  padx=11, pady=5, cursor="hand2")
    b.pack(side="left", padx=3)
    return b

# ──────────────────────────────────────────────────────────────
#  Code Tab
# ──────────────────────────────────────────────────────────────

class CodeTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        global _code_tab_ref
        _code_tab_ref = self
        self._build()

    def _build(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=10, pady=(8, 4))

        _mk_btn(bar, "▶ Run",         self._run,         GREEN)
        _mk_btn(bar, "💾 Save",        self._save,        ACCENT)
        _mk_btn(bar, "📂 Open Editor", self._open_editor, AMBER)
        _mk_btn(bar, "🗑 Clear",       self._clear,       FG2)

        self._lang_var = tk.StringVar(value="python")
        langs = sorted(LANG_RUNNERS.keys())
        ttk.OptionMenu(bar, self._lang_var, "python", *langs).pack(side="left", padx=6)

        paned = tk.PanedWindow(self, orient=tk.VERTICAL, bg=BG,
                               sashwidth=5, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Code editor pane
        cf = tk.Frame(paned, bg=BG)
        tk.Label(cf, text="  CODE", font=("Courier New", 8, "bold"),
                 bg=BG, fg=ACCENT, anchor="w").pack(fill="x")
        self.code_box = scrolledtext.ScrolledText(
            cf, bg="#050a0f", fg="#e0f0ff", font=("Courier New", 10),
            relief="flat", bd=0, insertbackground=ACCENT,
            selectbackground="#1a3050", wrap="none", undo=True,
        )
        self.code_box.pack(fill="both", expand=True)

        # Output pane
        of = tk.Frame(paned, bg=BG)
        tk.Label(of, text="  OUTPUT", font=("Courier New", 8, "bold"),
                 bg=BG, fg=GREEN, anchor="w").pack(fill="x")
        self.out_box = scrolledtext.ScrolledText(
            of, bg="#030a06", fg="#88ffaa", font=("Courier New", 10),
            relief="flat", bd=0, state="disabled",
            selectbackground="#1a3020", wrap="none",
        )
        self.out_box.pack(fill="both", expand=True)

        paned.add(cf, minsize=80)
        paned.add(of, minsize=60)
        paned.sash_place(0, 0, 310)

    # ── public API ─────────────────────────────────────────────
    def set_code(self, code: str, lang: str = "python"):
        self.code_box.delete("1.0", tk.END)
        self.code_box.insert("1.0", code)
        if lang in LANG_RUNNERS:
            self._lang_var.set(lang)

    def set_output(self, out: str):
        self.out_box.configure(state="normal")
        self.out_box.delete("1.0", tk.END)
        self.out_box.insert("1.0", out)
        self.out_box.configure(state="disabled")

    def get_code(self) -> str:
        return self.code_box.get("1.0", tk.END).strip()

    # ── buttons ────────────────────────────────────────────────
    def _run(self):
        code = self.get_code()
        if not code:
            return
        lang = self._lang_var.get()
        self.set_output("Running…")
        def _do():
            out = run_code(code, lang)
            self.set_output(out)
            first = out.splitlines()[0][:120] if out.strip() else "no output"
            speak(f"Done. {first}")
        threading.Thread(target=_do, daemon=True).start()

    def _save(self):
        name = simpledialog.askstring(
            "Save Code", "Filename (no extension):", parent=self
        )
        if name:
            path = save_code_to_file(self.get_code(), name, self._lang_var.get())
            _log_line(f"Code saved: {path}")
            speak(f"Saved as {name}.")

    def _open_editor(self):
        code = self.get_code()
        if not code:
            return
        path = save_code_to_file(
            code, f"jarvis_edit_{int(time.time())}", self._lang_var.get()
        )
        msg = open_code_in_editor(path)
        speak(msg)

    def _clear(self):
        self.code_box.delete("1.0", tk.END)
        self.set_output("")

# ──────────────────────────────────────────────────────────────
#  Main Window
# ──────────────────────────────────────────────────────────────

class JarvisApp(tk.Tk):
    def __init__(self):
        super().__init__()
        global _gui_log_widget, _status_var

        self.title("J.A.R.V.I.S  v3  —  AI Assistant")
        self.geometry("820x640")
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)   # hide, don't quit
        self.resizable(True, True)

        # Header
        hdr = tk.Frame(self, bg=BG)
        hdr.pack(fill="x", padx=18, pady=(12, 4))
        tk.Label(hdr, text="J.A.R.V.I.S",
                 font=("Courier New", 22, "bold"), bg=BG, fg=ACCENT).pack(side="left")
        tk.Label(hdr, text="  v3 — AI Voice + Coding Assistant",
                 font=("Courier New", 9), bg=BG, fg=FG2).pack(side="left", pady=6)

        badges = tk.Frame(hdr, bg=BG)
        badges.pack(side="right")
        tk.Label(badges,
                 text="🔒 ADMIN" if is_admin() else "⚠ NOT ADMIN",
                 font=("Courier New", 8, "bold"), bg=BG,
                 fg=GREEN if is_admin() else RED).pack(anchor="e")
        tk.Label(badges,
                 text="🟢 AUTO-START" if is_in_startup() else "⭘ NO AUTO-START",
                 font=("Courier New", 8), bg=BG,
                 fg=GREEN if is_in_startup() else FG2).pack(anchor="e")

        # Status bar
        _status_var = tk.StringVar(value='👂  Waiting for "Jarvis"…')
        tk.Label(self, textvariable=_status_var,
                 font=("Courier New", 10), bg=BG, fg=ACCENT,
                 anchor="w").pack(fill="x", padx=18)
        tk.Frame(self, bg="#1a2535", height=1).pack(fill="x", padx=18, pady=3)

        # Notebook
        style = ttk.Style()
        style.theme_use("default")
        style.configure("J.TNotebook",     background=BG, borderwidth=0)
        style.configure("J.TNotebook.Tab", background=BG2, foreground=FG2,
                        padding=[12, 4], font=("Courier New", 9, "bold"))
        style.map("J.TNotebook.Tab",
                  background=[("selected", BG3)],
                  foreground=[("selected", ACCENT)])

        nb = ttk.Notebook(self, style="J.TNotebook")
        nb.pack(fill="both", expand=True, padx=10, pady=4)

        lf = tk.Frame(nb, bg=BG)
        nb.add(lf, text="  📋 Activity  ")
        self._build_activity_tab(lf)

        nb.add(CodeTab(nb), text="  💻 Code  ")

        # Footer
        bot = tk.Frame(self, bg=BG)
        bot.pack(fill="x", padx=18, pady=(2, 6))
        self._sv = tk.BooleanVar(value=is_in_startup())
        tk.Checkbutton(bot, text="Auto-start with Windows",
                       variable=self._sv,
                       command=lambda: set_startup(self._sv.get()),
                       bg=BG, fg=FG2, selectcolor=BG,
                       activebackground=BG,
                       font=("Courier New", 9)).pack(side="left")
        api_ok = bool(ANTHROPIC_API_KEY)
        tk.Label(bot,
                 text="✓ API key ready" if api_ok else "✗ ANTHROPIC_API_KEY not set",
                 font=("Courier New", 8), bg=BG,
                 fg=GREEN if api_ok else RED).pack(side="right")

        # Text input
        inp = tk.Frame(self, bg=BG)
        inp.pack(fill="x", padx=18, pady=(0, 10))
        self._entry = tk.Entry(inp, bg=BG2, fg=FG, insertbackground=ACCENT,
                               font=("Courier New", 10), relief="flat", bd=4)
        self._entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self._entry.bind("<Return>", lambda _: self._send_text())
        tk.Button(inp, text="Send", command=self._send_text,
                  bg=BG2, fg=ACCENT, activebackground=BG3,
                  relief="flat", font=("Courier New", 9, "bold"),
                  padx=10, pady=6, cursor="hand2").pack(side="left")

    def _build_activity_tab(self, parent):
        global _gui_log_widget
        _gui_log_widget = scrolledtext.ScrolledText(
            parent, state="disabled", bg="#060b0f", fg="#9db4c0",
            font=("Courier New", 9), relief="flat", bd=0,
            selectbackground="#1a2535"
        )
        _gui_log_widget.pack(fill="both", expand=True, padx=8, pady=(6, 4))

        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=8, pady=(0, 6))
        _mk_btn(row, "▶ RESUME",    self._resume,   GREEN)
        _mk_btn(row, "⏸ PAUSE",    self._pause,    AMBER)
        _mk_btn(row, "⏺ RECORD",   self._record,   RED)
        _mk_btn(row, "⏹ STOP REC", self._stop_rec, FG2)
        _mk_btn(row, "📖 TEACH",   self._teach,    PURPLE)
        _mk_btn(row, "🔁 REPLAY",  self._replay,   "#66ccff")

    # ── callbacks ─────────────────────────────────────────────
    def _resume(self):
        _listening_active.set()
        _status_var.set('👂  Waiting for "Jarvis"…')
        _log_line("Listening resumed.")

    def _pause(self):
        _listening_active.clear()
        _status_var.set("⏸  Paused")
        _log_line("Listening paused.")

    def _record(self):
        # Give a 3-second countdown so user is ready
        def _countdown():
            for i in (3, 2, 1):
                if _status_var: _status_var.set(f"⏺  Recording starts in {i}…")
                speak(str(i))
                time.sleep(1)
            start_recording()
            if _status_var: _status_var.set("⏺  NOW RECORDING — do your actions…")
            speak("Recording. Do your actions now. Click Stop Rec when done.")
        threading.Thread(target=_countdown, daemon=True).start()

    def _stop_rec(self):
        name  = simpledialog.askstring(
            "Save Sequence", "Name for this sequence:", parent=self
        ) or "unnamed"
        steps = stop_recording(name)
        _status_var.set('👂  Waiting for "Jarvis"…')
        msg = f"Saved {len(steps)} actions as '{name}'."
        _log_line(msg)
        speak(msg)

    def _teach(self):
        phrase = simpledialog.askstring(
            "Teach", 'Voice phrase (e.g. "open my report"):', parent=self
        )
        if not phrase:
            return
        action = simpledialog.askstring(
            "Teach",
            'Action JSON:\n{"action":"open_app","args":{"name":"notepad"}}',
            parent=self,
        )
        if action:
            try:
                teach_command(phrase, json.loads(action))
                speak(f"Command '{phrase}' saved.")
            except (json.JSONDecodeError, ValueError):
                messagebox.showerror("Invalid JSON", "Please enter valid JSON.", parent=self)

    def _replay(self):
        skills = memory.get("learned_skills", {})
        if not skills:
            messagebox.showinfo("No Skills", "No learned skills yet.\nUse ⏺ RECORD to teach JARVIS a task.", parent=self)
            return
        names = list(skills.keys())
        name = simpledialog.askstring(
            "Run Skill",
            "Which skill to execute?\n" + "\n".join(f"  • {n}" for n in names),
            parent=self,
        )
        if name:
            threading.Thread(
                target=lambda: speak(execute_skill(name)),
                daemon=True
            ).start()

    def _send_text(self):
        text = self._entry.get().strip()
        if text:
            self._entry.delete(0, tk.END)
            _log_line(f"YOU (typed): {text}")
            threading.Thread(target=self._process, args=(text,), daemon=True).start()

    def _process(self, text: str):
        if _status_var:
            _status_var.set("⚙  Thinking…")
        resp = ask_jarvis(text)
        if len(resp) > 400 and "\n" in resp:
            speak_code_summary(resp)
        else:
            speak(resp)
        if _status_var:
            _status_var.set('👂  Waiting for "Jarvis"…')

# ══════════════════════════════════════════════════════════════════════════════
#  WAKE-WORD VOICE LOOP  (background thread)
# ══════════════════════════════════════════════════════════════════════════════

_listening_active  = threading.Event()
_listening_active.set()
_awaiting_reply    = threading.Event()   # set when JARVIS asked a question

def _is_question(text: str) -> bool:
    """Return True if JARVIS's response ends with a question."""
    t = text.strip().rstrip(".")
    question_words = ("what", "which", "who", "how", "when", "where", "why",
                      "can you", "could you", "would you", "should i", "do you",
                      "did you", "is that", "are you", "shall i")
    return (t.endswith("?") or
            any(t.lower().startswith(q) for q in question_words) or
            any(t.lower().endswith(q) for q in ("instead?", "correct?",
                "right?", "differently?", "name it?", "done?", "again?")))

def _voice_loop():
    r = sr.Recognizer()
    r.dynamic_energy_threshold = True
    r.energy_threshold = 300
    _log_line(f'Voice loop ready.  Wake word = "{WAKE_WORD}"')

    while True:
        if not _listening_active.is_set():
            time.sleep(0.5)
            continue

        # ── Wait for wake word ────────────────────────────────────────────────
        try:
            with sr.Microphone() as src:
                r.adjust_for_ambient_noise(src, duration=0.3)
                audio = r.listen(src, timeout=4, phrase_time_limit=10)
            text = r.recognize_google(audio).lower()
        except (sr.WaitTimeoutError, sr.UnknownValueError):
            continue
        except Exception as e:
            logging.debug(f"STT: {e}")
            time.sleep(0.5)
            continue

        if WAKE_WORD not in text:
            continue

        _log_line(f"🟢 Wake word: '{text}'")
        if _status_var:
            _status_var.set("🎙  Listening…")
        speak("Yes sir?")

        # Command may be in same phrase as wake word
        command = text.replace(WAKE_WORD, "").strip(" ,.")
        if not command:
            command = listen_once(timeout=8) or ""

        if not command:
            speak("I didn't catch that, sir.")
            if _status_var:
                _status_var.set('👂  Waiting for "Jarvis"…')
            continue

        # ── Handle the command, then keep conversing if JARVIS asks back ──────
        _handle_turn(r, command)

        if _status_var:
            _status_var.set('👂  Waiting for "Jarvis"…')


def _handle_turn(r: sr.Recognizer, command: str):
    """
    Process one command. If JARVIS's response is a question,
    automatically keep listening for the answer — no wake word needed.
    Repeats until JARVIS stops asking questions.
    """
    _log_line(f"YOU: {command}")

    # ── Skill / correction shortcut ───────────────────────────────────────
    if any(w in command for w in ("was wrong", "were wrong", "did that wrong",
                                   "opened wrong", "wrong way", "not right",
                                   "incorrect", "that's wrong", "thats wrong")):
        matched_skill = None
        for sname in memory.get("learned_skills", {}):
            if sname.lower() in command:
                matched_skill = sname
                break
        if matched_skill:
            speak(f"What should I have done differently for '{matched_skill}'?")
            # ← no wake word needed here
            correction = _listen_reply(r, timeout=12)
            if correction:
                result = _replan_skill(matched_skill,
                                       memory["learned_skills"][matched_skill],
                                       correction)
                speak(result)
            else:
                speak("I didn't hear a correction. Try again.")
            return
        elif _history:
            right = (command
                     .replace("that was wrong","").replace("you were wrong","")
                     .replace("the answer is","").replace("did that wrong","")
                     .strip())
            if right:
                teach_correction(_history[-1]["content"], right)
                speak("Correction noted, sir.")
            else:
                speak("What should the answer have been?")
                correction = _listen_reply(r, timeout=10)
                if correction:
                    teach_correction(_history[-1]["content"], correction)
                    speak("Got it, noted.")
            return

    # ── Normal command → get response (screen-aware) ──────────────────────
    if _status_var:
        _status_var.set("⚙  Thinking…")

    # Keywords that benefit most from seeing the screen
    SCREEN_KEYWORDS = (
        "click", "find", "where", "open", "close", "button", "window",
        "tab", "menu", "scroll", "select", "type in", "what's on",
        "what do you see", "look at", "on screen", "this page",
    )
    use_screen = any(kw in command.lower() for kw in SCREEN_KEYWORDS)

    if use_screen:
        _log_line("📸 Capturing screen for context…")
        resp = screen_aware_ask(command)
    else:
        resp = ask_jarvis(command)

    if len(resp) > 400 and "\n" in resp:
        speak_code_summary(resp)
    else:
        speak(resp)

    # ── If response is a question, keep listening without wake word ────────
    MAX_FOLLOWUPS = 5   # prevent infinite loops
    followups = 0
    while _is_question(resp) and followups < MAX_FOLLOWUPS:
        if _status_var:
            _status_var.set("👂  Listening for your answer…")
        _log_line("(Waiting for follow-up — no wake word needed)")

        reply = _listen_reply(r, timeout=10)

        if not reply:
            speak("I didn't catch that. Just say 'Jarvis' when you're ready to continue.")
            break

        if WAKE_WORD in reply:
            # User said "Jarvis" — treat as new command, strip wake word
            reply = reply.replace(WAKE_WORD, "").strip(" ,.")

        _log_line(f"YOU (reply): {reply}")
        if _status_var:
            _status_var.set("⚙  Thinking…")

        resp = ask_jarvis(reply)

        if len(resp) > 400 and "\n" in resp:
            speak_code_summary(resp)
        else:
            speak(resp)

        followups += 1


def _listen_reply(r: sr.Recognizer, timeout: int = 10) -> str | None:
    """
    Listen for a single reply WITHOUT requiring the wake word.
    Waits for JARVIS to finish speaking first.
    """
    # Wait for TTS to finish before opening mic (avoids JARVIS hearing itself)
    _tts_queue.join()
    time.sleep(0.3)

    try:
        with sr.Microphone() as src:
            r.adjust_for_ambient_noise(src, duration=0.2)
            audio = r.listen(src, timeout=timeout, phrase_time_limit=15)
        return r.recognize_google(audio).lower()
    except (sr.WaitTimeoutError, sr.UnknownValueError):
        return None
    except Exception as e:
        logging.debug(f"Reply STT: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    # 1 — Elevate to admin (re-launches with UAC if needed)
    elevate_to_admin()

    # 2 — Register auto-start
    if not is_in_startup():
        set_startup(True)

    # 3 — Build GUI (must be on main thread)
    app = JarvisApp()

    # 4 — Start voice listener
    threading.Thread(target=_voice_loop, daemon=True, name="voice-loop").start()

    # 5 — Greet after short delay (GUI needs to paint first)
    def _greet():
        time.sleep(1.2)
        parts = [
            "JARVIS version 3 online.",
            "Running as Administrator." if is_admin() else "Warning: no admin rights.",
            "Auto-start enabled." if is_in_startup() else "",
            "Coding engine active." if ANTHROPIC_API_KEY else
            "Warning: API key not set. Say Jarvis to test voice.",
            "Say 'Jarvis' at any time, sir.",
        ]
        speak(" ".join(p for p in parts if p))
    threading.Thread(target=_greet, daemon=True, name="greeter").start()

    # 6 — Run GUI event loop (blocks until window is destroyed)
    app.mainloop()

if __name__ == "__main__":
    main()
