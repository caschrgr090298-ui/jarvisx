"""
jarvis_updater.py
-----------------
Drop this file in the same folder as jarvis.py.
JARVIS calls check_for_update() on every startup.

HOW TO SET UP (one time):
  1. Go to https://github.com and create a free account
  2. Create a new repository called "jarvis"
  3. Upload your jarvis.py file to it
  4. Click jarvis.py → click Raw → copy that URL
  5. Paste it below as JARVIS_RAW_URL

Every time you want to push an update:
  - Edit jarvis.py on GitHub
  - Change VERSION_URL to point to a version.txt file (see below)
  - JARVIS will auto-download the new file next launch
"""

import os
import sys
import json
import shutil
import logging
import subprocess
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════════
#  ★  CONFIGURE THESE TWO LINES  ★
# ══════════════════════════════════════════════════════════════════════════════

# Raw URL to your jarvis.py on GitHub
# Example: "https://raw.githubusercontent.com/YourUsername/jarvis/main/jarvis.py"
JARVIS_RAW_URL = "YOUR_GITHUB_RAW_URL_HERE"

# Raw URL to a version.txt file on GitHub (just contains a number like "3.1")
# Example: "https://raw.githubusercontent.com/YourUsername/jarvis/main/version.txt"
VERSION_URL    = "YOUR_VERSION_TXT_URL_HERE"

# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
LOCAL_VERSION  = os.path.join(BASE_DIR, "jarvis_version.txt")
JARVIS_PY      = os.path.join(BASE_DIR, "jarvis.py")
BACKUP_PY      = os.path.join(BASE_DIR, "jarvis_backup.py")
UPDATE_LOG     = os.path.join(BASE_DIR, "jarvis_update.log")

logging.basicConfig(
    filename=UPDATE_LOG,
    level=logging.INFO,
    format="%(asctime)s  %(message)s"
)

def _get_local_version() -> str:
    """Read the locally stored version number."""
    if os.path.exists(LOCAL_VERSION):
        try:
            with open(LOCAL_VERSION) as f:
                return f.read().strip()
        except Exception:
            pass
    return "0.0"

def _save_local_version(version: str):
    with open(LOCAL_VERSION, "w") as f:
        f.write(version.strip())

def _fetch(url: str, timeout: int = 10) -> str | None:
    """Fetch text content from a URL using requests or urllib fallback."""
    # Try requests first
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except ImportError:
        pass
    except Exception as e:
        logging.error(f"requests fetch failed: {e}")
        return None

    # Fallback to urllib (built-in, always available)
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        logging.error(f"urllib fetch failed: {e}")
        return None

def check_for_update(speak_fn=None) -> bool:
    """
    Called on every JARVIS startup.
    - Fetches remote version number
    - If newer than local, downloads new jarvis.py
    - Backs up old version first
    - Restarts JARVIS with new file
    Returns True if an update was applied (triggers restart).
    """

    # Skip if not configured yet
    if "YOUR_GITHUB" in JARVIS_RAW_URL or "YOUR_VERSION" in VERSION_URL:
        logging.info("Updater not configured — skipping update check.")
        return False

    logging.info("Checking for updates…")

    # ── Fetch remote version ───────────────────────────────────────────────
    remote_version = _fetch(VERSION_URL)
    if not remote_version:
        logging.warning("Could not reach version server — skipping update.")
        return False

    remote_version = remote_version.strip()
    local_version  = _get_local_version()

    logging.info(f"Local: {local_version}  Remote: {remote_version}")

    # ── Compare versions ───────────────────────────────────────────────────
    try:
        remote_parts = [int(x) for x in remote_version.split(".")]
        local_parts  = [int(x) for x in local_version.split(".")]
        is_newer     = remote_parts > local_parts
    except ValueError:
        # Non-numeric version string — compare as plain strings
        is_newer = remote_version != local_version

    if not is_newer:
        logging.info("Already up to date.")
        return False

    # ── New version available — download it ────────────────────────────────
    logging.info(f"Update available: {local_version} → {remote_version}")

    if speak_fn:
        speak_fn(f"Update available. Downloading version {remote_version}.")

    new_code = _fetch(JARVIS_RAW_URL, timeout=30)
    if not new_code:
        logging.error("Failed to download new jarvis.py — aborting update.")
        if speak_fn:
            speak_fn("Update download failed. Continuing with current version.")
        return False

    # ── Syntax check the downloaded file before replacing ─────────────────
    import tempfile, py_compile
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False,
                                      mode="w", encoding="utf-8")
    tmp.write(new_code); tmp.close()
    try:
        py_compile.compile(tmp.name, doraise=True)
    except py_compile.PyCompileError as e:
        logging.error(f"Downloaded file has syntax errors — aborting: {e}")
        os.unlink(tmp.name)
        if speak_fn:
            speak_fn("Update file has errors. Keeping current version.")
        return False
    finally:
        try: os.unlink(tmp.name)
        except: pass

    # ── Backup current version ─────────────────────────────────────────────
    if os.path.exists(JARVIS_PY):
        shutil.copy2(JARVIS_PY, BACKUP_PY)
        logging.info(f"Backed up current version to {BACKUP_PY}")

    # ── Write new version ──────────────────────────────────────────────────
    with open(JARVIS_PY, "w", encoding="utf-8") as f:
        f.write(new_code)

    _save_local_version(remote_version)
    logging.info(f"Updated to version {remote_version} at {datetime.now()}")

    if speak_fn:
        speak_fn(f"Updated to version {remote_version}. Restarting now.")

    # ── Restart JARVIS with new file ───────────────────────────────────────
    import time
    time.sleep(2)   # let TTS finish speaking
    os.execv(sys.executable, [sys.executable] + sys.argv)

    return True   # never reached after execv, but here for clarity
