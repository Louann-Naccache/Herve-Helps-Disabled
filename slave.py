"""
AI-controlled Windows automation agent (autonomous mode, no confirmations).

Four families of tools:
  - TEXT tools (fast, no screenshot needed): run_shell_command, open_application,
    list_directory, read_text_file, write_text_file, send_email, wait.
    These act directly through cmd.exe / the filesystem / Outlook COM, so they
    are far more reliable than clicking on-screen icons and don't need a
    screenshot round-trip.
  - BROWSER tools (Playwright — for ANY website): browser_navigate,
    browser_list_elements, browser_click_element_index, browser_click_grid_cell,
    browser_get_text. These click real DOM elements or compute grid-cell
    positions from an element's actual bounding box — no pixel-coordinate
    guessing at all.
  - DESKTOP tools (pywinauto / Windows UI Automation — for native apps, desktop
    icons, Start menu): desktop_list_elements, desktop_click_element_index.
    Same idea as the browser tools, applied to Windows itself: real element
    positions read from the OS accessibility tree, not guessed from a picture.
  - GUI tools (vision, LAST RESORT): move_and_click, type_text, press_key,
    scroll. move_and_click is the only one that actually guesses a pixel
    position — used only when nothing above finds the target (custom-drawn
    content with no accessibility tree, e.g. a canvas game or a broken
    Electron app). gpt-4o is a general vision model, not trained for precise
    GUI coordinate grounding, so this path is inherently the least reliable
    one and should be a small minority of actions.

To keep things fast, a screenshot + image tokens are only sent to the model
right after a GUI action (or on step 1). After a text/browser/desktop-tool
action, the model gets the raw text result immediately and can chain the next
tool call without waiting on a new image, which is the main source of latency.

Every tool still requires `observation` + `reasoning` fields so the model
verifies the previous step actually worked instead of barreling forward.

Requirements (Python 3.10, Windows):
    pip install openai pyautogui pillow pywin32 playwright pywinauto
    playwright install chromium

Setup:
    setx OPENAI_API_KEY "sk-..."   (restart terminal after)
    -- or for the current session only --
    $env:OPENAI_API_KEY="sk-..."

Run:
    python agent_controller.py "Ouvre Chrome et va sur duckduckgo.com"

SAFETY NOTE:
  No confirmation prompts are used, per configuration. A minimal, silent
  blocklist rejects a handful of catastrophic shell patterns (full disk
  format/wipe, diskpart, shutdown) so a model hallucination can't nuke the
  machine. Everything else runs immediately and autonomously. Remove
  `is_blocked_command` if you truly want zero limits.
"""

import base64
import io
import json
import random
import re
import subprocess
import sys
import time
from pathlib import Path

# Make this process DPI-aware BEFORE any pyautogui/screenshot calls happen.
# On Windows, if the process isn't DPI-aware, screenshots may be captured in
# physical pixels while pyautogui click coordinates are interpreted in
# DPI-scaled logical pixels (or vice versa), producing a constant offset
# between where the model "sees" something and where the click lands.
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import pyautogui
from openai import OpenAI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL = "gpt-4o"
MAX_STEPS = 60
BASE_STEP_DELAY = 0.3
NAV_SETTLE_DELAY = 2.0
MAX_CONSECUTIVE_FAILURES = 3
FAILURE_ESCALATION_STEP = 2.0
SCREENSHOT_MAX_DIM = 1536
SHELL_TIMEOUT_SECONDS = 20

# Files/folders the model can read/write without restriction (autonomy mode).
# Defaults to the user's home dir so it covers Documents/Desktop/Downloads/etc.
USER_HOME = Path.home()

# Known desktop apps: name -> shell "start" command. Extend freely.
APP_LAUNCH_COMMANDS = {
    "chrome": 'start chrome',
    "google chrome": 'start chrome',
    "outlook": 'start outlook',
    "excel": 'start excel',
    "word": 'start winword',
    "notepad": 'start notepad',
    "explorer": 'start explorer',
    "file explorer": 'start explorer',
    "calculator": 'start calc',
    "paint": 'start mspaint',
    "edge": 'start msedge',
    "task manager": 'start taskmgr',
}

# Minimal, silent safety net — blocks only genuinely catastrophic patterns.
_BLOCKED_PATTERNS = [
    r"\bformat\s+[a-z]:",
    r"\bdiskpart\b",
    r"\bshutdown\b",
    r"\bvssadmin\b.*\bdelete\b",
    r"\bcipher\s+/w",
    r"\bdel\s+/[a-z]*\s*/[a-z]*\s*/[a-z]*\s*[a-z]:\\+\s*$",   # e.g. del /f /s /q C:\
    r"\brd\s+/s\s+/q\s+[a-z]:\\+\s*$",                         # e.g. rd /s /q C:\
    r"\bbcdedit\b",
]


def is_blocked_command(cmd: str) -> bool:
    lowered = cmd.lower()
    return any(re.search(p, lowered) for p in _BLOCKED_PATTERNS)


pyautogui.FAILSAFE = True     # move mouse to a screen corner to abort
pyautogui.PAUSE = 0.05

client = OpenAI(api_key="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")

MAX_TOKENS_PER_CALL = 800


def call_chat_completion_with_retry(max_retries: int = 5, **kwargs):
    """Wraps client.chat.completions.create with backoff on rate-limit (429) errors,
    instead of letting them bubble up and failing the whole step."""
    kwargs.setdefault("max_tokens", MAX_TOKENS_PER_CALL)
    delay = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            msg = str(e)
            if "rate_limit" in msg.lower() or "429" in msg:
                wait = delay * attempt
                print(f"[rate-limit] hit, retrying in {wait:.1f}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Max retries exceeded due to persistent rate limiting.")

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
_COMMON_PROPS = {
    "observation": {
        "type": "string",
        "description": (
            "What you currently know (from the last tool result or screenshot), and whether "
            "the previous action actually worked. Call out failures explicitly."
        ),
    },
    "reasoning": {
        "type": "string",
        "description": "Why this specific action is the right next step given the observation.",
    },
}

TEXT_TOOL_NAMES = {
    "run_shell_command",
    "open_application",
    "list_directory",
    "read_text_file",
    "write_text_file",
    "send_email",
    "browser_navigate",
    "browser_list_elements",
    "browser_click_element_index",
    "browser_click_grid_cell",
    "browser_get_text",
    "desktop_list_elements",
    "desktop_click_element_index",
    "wait",
}
GUI_TOOL_NAMES = {"move_and_click", "type_text", "press_key", "scroll"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": (
                "Open a known desktop application directly via the shell (fast, reliable — "
                "no clicking icons). Prefer this over move_and_click whenever you need to open "
                "an app. Known names: " + ", ".join(sorted(APP_LAUNCH_COMMANDS)) + ". "
                "Any other name is attempted via `start <name>` as a fallback."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "app_name": {"type": "string"}},
                "required": ["observation", "reasoning", "app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": (
                "Run a raw command through cmd.exe and get back stdout/stderr. Use this for "
                "anything doable from the command line: opening files/folders, running "
                "scripts, checking if a process is running (tasklist), killing one "
                "(taskkill), moving/copying files, etc. Prefer this over GUI clicking "
                "whenever a command-line equivalent exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "command": {"type": "string"}},
                "required": ["observation", "reasoning", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files/folders at a path (defaults to the user's home directory).",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "path": {"type": "string"}},
                "required": ["observation", "reasoning", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "Read the contents of a text-based file (txt, csv, md, json, log, etc.).",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "path": {"type": "string"}},
                "required": ["observation", "reasoning", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_text_file",
            "description": "Write (overwrite or append) text content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    **_COMMON_PROPS,
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"]},
                },
                "required": ["observation", "reasoning", "path", "content", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "Send an email through the local Outlook desktop app (must be installed and "
                "configured). Sends immediately — no draft/confirmation step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_COMMON_PROPS,
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {"type": "string"},
                },
                "required": ["observation", "reasoning", "to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": (
                "Open a URL in a real, controllable browser (Playwright — NOT the default OS "
                "browser). ALWAYS use this instead of `open_application('chrome')` or "
                "`run_shell_command('start <url>')` whenever the task involves a website — it "
                "lets you click exact elements instead of guessing pixels."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "url": {"type": "string"}},
                "required": ["observation", "reasoning", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_list_elements",
            "description": (
                "List visible clickable/interactive elements on the current page (buttons, "
                "links, grid cells, canvases, etc.) with their index, tag, and text. ALWAYS call "
                "this before clicking anything on a webpage — then click by index "
                "(`browser_click_element_index`) or use `browser_click_grid_cell` for a grid "
                "container. This replaces guessing pixel coordinates entirely for web content."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS},
                "required": ["observation", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click_element_index",
            "description": "Click the element at the given index, as returned by browser_list_elements.",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "index": {"type": "integer"}},
                "required": ["observation", "reasoning", "index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click_grid_cell",
            "description": (
                "Click a specific cell of an R x C grid (e.g. a 3x3 tic-tac-toe board) inside "
                "the element at container_index (from browser_list_elements — often a canvas or "
                "the grid's outer div). Coordinates are computed exactly from the element's real "
                "bounding box, not estimated. row/col are 0-indexed from the top-left."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_COMMON_PROPS,
                    "container_index": {"type": "integer"},
                    "row": {"type": "integer"},
                    "col": {"type": "integer"},
                    "rows": {"type": "integer"},
                    "cols": {"type": "integer"},
                },
                "required": ["observation", "reasoning", "container_index", "row", "col", "rows", "cols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_text",
            "description": (
                "Get the current page's visible text content. Use this to read game state, "
                "confirm a move registered, check for error messages, etc. — much more reliable "
                "than trying to read small text from a screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS},
                "required": ["observation", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_list_elements",
            "description": (
                "List real interactive elements (buttons, menu items, icons, list items, etc.) "
                "from the OS accessibility tree — either the active foreground app window "
                "(scope='foreground') or the desktop icons (scope='desktop'). ALWAYS try this "
                "before move_and_click for native apps, the Start menu, or desktop icons — it "
                "gives exact element positions with no coordinate guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_COMMON_PROPS,
                    "scope": {"type": "string", "enum": ["foreground", "desktop"]},
                },
                "required": ["observation", "reasoning", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click_element_index",
            "description": "Click the element at the given index, as returned by desktop_list_elements (same scope).",
            "parameters": {
                "type": "object",
                "properties": {
                    **_COMMON_PROPS,
                    "index": {"type": "integer"},
                    "scope": {"type": "string", "enum": ["foreground", "desktop"]},
                },
                "required": ["observation", "reasoning", "index", "scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_and_click",
            "description": (
                "Move the mouse to coordinates and click. IMPORTANT: give coordinates relative "
                "to the screenshot IMAGE you were just shown (not the physical screen — it may "
                "be downscaled, and is automatically rescaled for you). The screenshot has a red "
                "grid every 100px with axis labels — use the nearest gridlines to estimate the "
                "target's coordinates precisely, since the click happens exactly where you say. "
                "Use only for in-app GUI interactions with no CLI equivalent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_COMMON_PROPS,
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "double"]},
                    "target_description": {
                        "type": "string",
                        "description": (
                            "Short, specific description of the exact element being clicked "
                            "(e.g. 'the top-left square of the tic-tac-toe grid', 'the blue OK "
                            "button'), for your own reasoning/logging clarity."
                        ),
                    },
                },
                "required": ["observation", "reasoning", "x", "y", "button", "target_description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text at the current cursor/focus location.",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "text": {"type": "string"}},
                "required": ["observation", "reasoning", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a single key or key combo, e.g. 'enter', 'ctrl+s', 'esc'.",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "keys": {"type": "string"}},
                "required": ["observation", "reasoning", "keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the mouse wheel. Positive = up, negative = down.",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "amount": {"type": "integer"}},
                "required": ["observation", "reasoning", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Do nothing for N seconds (e.g. waiting for a page/app to finish loading).",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "seconds": {"type": "number"}},
                "required": ["observation", "reasoning", "seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_last_action",
            "description": "Use when the previous action clearly failed and you want to redo it differently.",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "corrected_action": {"type": "string"}},
                "required": ["observation", "reasoning", "corrected_action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Call when the task is fully, verifiably complete (or truly unrecoverable).",
            "parameters": {
                "type": "object",
                "properties": {**_COMMON_PROPS, "summary": {"type": "string"}},
                "required": ["observation", "reasoning", "summary"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Click helpers
# ---------------------------------------------------------------------------
def clamp_to_screen(real_x: int, real_y: int) -> tuple[int, int]:
    """Keep coordinates a couple pixels away from the exact screen corners so they never
    accidentally land on a pyautogui FAILSAFE trigger pixel and abort the click."""
    screen_w, screen_h = pyautogui.size()
    buffer = 3
    cx = max(buffer, min(real_x, screen_w - 1 - buffer))
    cy = max(buffer, min(real_y, screen_h - 1 - buffer))
    return cx, cy


def get_mouse_ground_truth_text() -> str:
    """Real cursor position, independent of anything the model perceives from the screenshot."""
    mx, my = pyautogui.position()
    if LAST_SCREENSHOT_SIZE is not None:
        img_w, img_h = LAST_SCREENSHOT_SIZE
        screen_w, screen_h = pyautogui.size()
        approx_img_x = round(mx * img_w / screen_w)
        approx_img_y = round(my * img_h / screen_h)
        return (
            f"Ground-truth mouse cursor position (NOT from image analysis, actual OS-reported "
            f"position): real screen ({mx}, {my}), which corresponds to approximately "
            f"({approx_img_x}, {approx_img_y}) in the screenshot image below."
        )
    return f"Ground-truth mouse cursor position (actual OS-reported position): real screen ({mx}, {my})."

# Tracks the dimensions of the last screenshot actually sent to the model,
# so click coordinates (given relative to that image) can be scaled back to
# real screen pixels. Without this, a downscaled screenshot + coordinates
# treated as full-resolution causes systematic misclicks.
LAST_SCREENSHOT_SIZE = None  # (img_w, img_h)
GRID_STEP = 100  # pixels, in the (possibly downscaled) image sent to the model


def take_screenshot_b64() -> tuple[str, int, int]:
    global LAST_SCREENSHOT_SIZE
    img = pyautogui.screenshot()
    img.thumbnail((SCREENSHOT_MAX_DIM, SCREENSHOT_MAX_DIM))
    img = img.convert("RGB")

    # Draw a light coordinate grid + labels so the model can anchor its
    # coordinate guesses instead of eyeballing raw pixels.
    draw = ImageDraw.Draw(img)
    w, h = img.size
    grid_color = (255, 0, 0)
    for gx in range(0, w, GRID_STEP):
        draw.line([(gx, 0), (gx, h)], fill=grid_color, width=1)
        draw.text((gx + 2, 2), str(gx), fill=grid_color)
    for gy in range(0, h, GRID_STEP):
        draw.line([(0, gy), (w, gy)], fill=grid_color, width=1)
        draw.text((2, gy + 2), str(gy), fill=grid_color)

    LAST_SCREENSHOT_SIZE = (w, h)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8"), w, h


def scale_to_real_screen(x: int, y: int) -> tuple[int, int]:
    """Convert coordinates given relative to the last screenshot image into real screen pixels."""
    if LAST_SCREENSHOT_SIZE is None:
        return x, y
    img_w, img_h = LAST_SCREENSHOT_SIZE
    screen_w, screen_h = pyautogui.size()
    scale_x = screen_w / img_w
    scale_y = screen_h / img_h
    return round(x * scale_x), round(y * scale_y)


# ---------------------------------------------------------------------------
# Human-like GUI helpers
# ---------------------------------------------------------------------------
def human_move_and_click(x: int, y: int, button: str = "left", target_description: str = ""):
    approx_x, approx_y = scale_to_real_screen(x, y)
    real_x, real_y = clamp_to_screen(approx_x, approx_y)
    duration = random.uniform(0.3, 0.9)
    jitter_x, jitter_y = real_x + random.randint(-1, 1), real_y + random.randint(-1, 1)
    pyautogui.moveTo(jitter_x, jitter_y, duration=duration, tween=pyautogui.easeInOutQuad)
    time.sleep(random.uniform(0.05, 0.15))
    pyautogui.doubleClick() if button == "double" else pyautogui.click(button=button)
    return real_x, real_y


def human_type(text: str):
    for ch in text:
        pyautogui.write(ch, interval=random.uniform(0.02, 0.09))
        if ch in ".,!?\n" and random.random() < 0.3:
            time.sleep(random.uniform(0.1, 0.3))


def resolve_path(raw_path: str) -> Path:
    p = Path(raw_path).expanduser()
    return p if p.is_absolute() else (USER_HOME / p)


# ---------------------------------------------------------------------------
# Text-tool implementations (no screenshot needed)
# ---------------------------------------------------------------------------
def run_shell_command(command: str) -> str:
    if is_blocked_command(command):
        return f"BLOCKED: '{command}' matches a catastrophic-command pattern and was not run."
    try:
        result = subprocess.run(
            ["cmd", "/c", command],
            capture_output=True, text=True, timeout=SHELL_TIMEOUT_SECONDS, shell=False,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        return f"exit_code={result.returncode}\nstdout:\n{out}\nstderr:\n{err}"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {SHELL_TIMEOUT_SECONDS}s: {command}"
    except Exception as e:
        return f"Error running command: {e}"


def open_application(app_name: str) -> str:
    key = app_name.strip().lower()
    cmd = APP_LAUNCH_COMMANDS.get(key, f'start "" "{app_name}"')
    result = run_shell_command(cmd)
    return f"Launch command used: `{cmd}`\n{result}"


def list_directory(path: str) -> str:
    target = resolve_path(path or ".")
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [f"{'[DIR] ' if e.is_dir() else '      '}{e.name}" for e in entries]
        return f"Contents of {target}:\n" + ("\n".join(lines) if lines else "(empty)")
    except Exception as e:
        return f"Error listing {target}: {e}"


def read_text_file(path: str) -> str:
    target = resolve_path(path)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        if len(content) > 8000:
            content = content[:8000] + "\n...[truncated]..."
        return f"Contents of {target}:\n{content}"
    except Exception as e:
        return f"Error reading {target}: {e}"


def write_text_file(path: str, content: str, mode: str) -> str:
    target = resolve_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} chars to {target} (mode={mode})."
    except Exception as e:
        return f"Error writing {target}: {e}"


def send_email(to: str, subject: str, body: str, cc: str = "") -> str:
    try:
        import win32com.client  # pywin32
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.To = to
        if cc:
            mail.CC = cc
        mail.Subject = subject
        mail.Body = body
        mail.Send()
        return f"Email sent to {to} (subject: {subject!r})."
    except ImportError:
        return "Error: pywin32 not installed. Run: pip install pywin32"
    except Exception as e:
        return f"Error sending email via Outlook: {e}"


# ---------------------------------------------------------------------------
# Browser automation (Playwright) — for ANY website interaction.
#
# Root cause of the click-precision problems: gpt-4o is a general vision model,
# not trained for pixel-accurate GUI coordinate grounding. No amount of
# grid/zoom/calibration math fixes that reliably. For web content, the actual
# fix is to stop guessing pixels entirely: Playwright drives a real browser
# and knows the exact position of every DOM element, so clicks are exact by
# construction, not estimated.
# ---------------------------------------------------------------------------
_pw = None
_browser = None
_page = None

# Cache of the last browser_list_elements() result (with exact page coordinates),
# so browser_click_element_index/browser_click_grid_cell click the SAME elements
# that were listed, instead of re-querying the DOM with a CSS selector (which
# could return elements in a different order/set if the page changed at all
# between the two calls).
_last_browser_elements: list = []

_LIST_ELEMENTS_JS = """
() => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const s = window.getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) === 0) return false;
    return true;
  };
  const candidates = new Set();
  document.querySelectorAll(
    'button, a, input, select, textarea, [role=button], [onclick], [tabindex], td, canvas, svg'
  ).forEach(el => candidates.add(el));
  // Heuristic fallback: ANY element with a pointer cursor is probably clickable,
  // regardless of tag/class naming — catches custom game boards, div-based grids,
  // etc. that don't match a fixed list of expected class names.
  document.querySelectorAll('div, span, li, g, rect, circle, path, img').forEach(el => {
    if (window.getComputedStyle(el).cursor === 'pointer') candidates.add(el);
  });
  const results = [];
  for (const el of candidates) {
    if (!isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 40);
    results.push({
      index: results.length,
      tag: el.tagName.toLowerCase(),
      text: text,
      cx: Math.round(r.x + r.width / 2),
      cy: Math.round(r.y + r.height / 2),
      width: Math.round(r.width),
      height: Math.round(r.height),
    });
    if (results.length >= 80) break;
  }
  return results;
}
"""


def _ensure_browser():
    global _pw, _browser, _page
    if _page is not None:
        return _page
    from playwright.sync_api import sync_playwright
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(headless=False)
    _page = _browser.new_page(viewport={"width": 1280, "height": 800})
    return _page


def browser_navigate(url: str) -> str:
    global _last_browser_elements
    try:
        page = _ensure_browser()
        page.goto(url, wait_until="load", timeout=20000)
        _last_browser_elements = []  # stale after navigation
        return f"Navigated to {page.url}"
    except Exception as e:
        return f"Error navigating to {url}: {e}"


def browser_list_elements() -> str:
    global _last_browser_elements
    try:
        page = _ensure_browser()
        elements = page.evaluate(_LIST_ELEMENTS_JS)
        _last_browser_elements = elements
        if not elements:
            return "No interactive elements found on the current page."
        # Show index/tag/text/size to the model; cx/cy are kept internally and
        # used directly on click, not surfaced as something to reason about.
        display = [
            {"index": e["index"], "tag": e["tag"], "text": e["text"], "width": e["width"], "height": e["height"]}
            for e in elements
        ]
        return json.dumps(display)
    except Exception as e:
        return f"Error listing elements: {e}"


def browser_click_element_index(index: int) -> str:
    """Click the exact element at `index`, using the cached position from the last
    browser_list_elements() call — not a fresh DOM query, so it can't drift."""
    try:
        if not _last_browser_elements:
            return "Error: call browser_list_elements first, no cached elements to click."
        if index < 0 or index >= len(_last_browser_elements):
            return f"Error: index {index} out of range (0..{len(_last_browser_elements) - 1})."
        page = _ensure_browser()
        el = _last_browser_elements[index]
        page.mouse.click(el["cx"], el["cy"])
        return f"Clicked element index {index} ({el['tag']!r}, text={el['text']!r})."
    except Exception as e:
        return f"Error clicking element index {index}: {e}"


def browser_click_grid_cell(container_index: int, row: int, col: int, rows: int, cols: int) -> str:
    """Click a cell of an R x C grid inside the cached element at `container_index`,
    using its exact real bounding box (from the last browser_list_elements() call) —
    pure math on real coordinates, no vision estimate and no re-query drift."""
    try:
        if not _last_browser_elements:
            return "Error: call browser_list_elements first, no cached elements available."
        if container_index < 0 or container_index >= len(_last_browser_elements):
            return f"Error: container index {container_index} out of range (0..{len(_last_browser_elements) - 1})."
        page = _ensure_browser()
        el = _last_browser_elements[container_index]
        x0, y0 = el["cx"] - el["width"] / 2, el["cy"] - el["height"] / 2
        cell_w, cell_h = el["width"] / cols, el["height"] / rows
        x = x0 + cell_w * (col + 0.5)
        y = y0 + cell_h * (row + 0.5)
        page.mouse.click(x, y)
        return (
            f"Clicked grid cell (row={row}, col={col}) of a {rows}x{cols} grid inside element "
            f"{container_index}, at page coords ({x:.0f}, {y:.0f})."
        )
    except Exception as e:
        return f"Error clicking grid cell: {e}"


def browser_get_text() -> str:
    try:
        page = _ensure_browser()
        text = page.evaluate("() => document.body.innerText")
        return text[:4000] + ("...[truncated]" if len(text) > 4000 else "")
    except Exception as e:
        return f"Error reading page text: {e}"


# ---------------------------------------------------------------------------
# Desktop UI Automation (pywinauto) — for native apps, the desktop, and the
# Start menu. Same principle as the browser tools: get REAL element positions
# from the OS accessibility tree instead of asking the vision model to guess
# pixels. This covers most of what move_and_click was previously used for.
# ---------------------------------------------------------------------------
_UIA_INTERACTIVE_TYPES = {
    "Button", "MenuItem", "ListItem", "CheckBox", "RadioButton",
    "Hyperlink", "TabItem", "Edit", "ComboBox", "TreeItem", "SplitButton",
}


def _get_uia_window(scope: str):
    import win32gui
    from pywinauto import Application

    if scope == "desktop":
        hwnd = win32gui.FindWindow("Progman", None)
        if not hwnd:
            hwnd = win32gui.FindWindow("WorkerW", None)
    else:
        hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError(f"Could not find a window handle for scope={scope!r}")
    app = Application(backend="uia").connect(handle=hwnd)
    return app.window(handle=hwnd)


def _uia_interactive_descendants(scope: str):
    win = _get_uia_window(scope)
    items = []
    for ctrl in win.descendants():
        try:
            ctype = ctrl.element_info.control_type
            if ctype not in _UIA_INTERACTIVE_TYPES:
                continue
            rect = ctrl.rectangle()
            if rect.width() < 2 or rect.height() < 2:
                continue
            try:
                if not ctrl.is_visible():
                    continue
            except Exception:
                pass  # not all control types support is_visible(); don't exclude on that basis
            items.append(ctrl)
            if len(items) >= 100:
                break
        except Exception:
            continue
    return items


def desktop_list_elements(scope: str = "foreground") -> str:
    """scope: 'foreground' = active app window, 'desktop' = desktop icons."""
    try:
        ctrls = _uia_interactive_descendants(scope)
        results = []
        for i, ctrl in enumerate(ctrls):
            try:
                name = (ctrl.window_text() or ctrl.element_info.name or "").strip()[:40]
                results.append({"index": i, "type": ctrl.element_info.control_type, "name": name})
            except Exception:
                continue
        if not results:
            return f"No interactive elements found (scope={scope})."
        return json.dumps(results)
    except Exception as e:
        return f"Error listing desktop elements (scope={scope}): {e}"


def desktop_click_element_index(index: int, scope: str = "foreground") -> str:
    try:
        ctrls = _uia_interactive_descendants(scope)
        if index >= len(ctrls):
            return f"Error: index {index} out of range ({len(ctrls)} elements found, scope={scope})."
        ctrl = ctrls[index]
        name = ctrl.window_text() or ctrl.element_info.name or ""
        ctrl.click_input()  # moves to the element's real center and clicks — exact, no guessing
        return f"Clicked element index {index} ({ctrl.element_info.control_type!r}, {name!r}), scope={scope}."
    except Exception as e:
        return f"Error clicking desktop element index {index} (scope={scope}): {e}"


def execute_action(name: str, args: dict) -> tuple[str, bool]:
    """Returns (result_text, likely_triggers_navigation_or_needs_settle)."""
    if name == "open_application":
        return (open_application(args["app_name"]), True)
    if name == "run_shell_command":
        return (run_shell_command(args["command"]), False)
    if name == "list_directory":
        return (list_directory(args.get("path", str(USER_HOME))), False)
    if name == "read_text_file":
        return (read_text_file(args["path"]), False)
    if name == "write_text_file":
        return (write_text_file(args["path"], args["content"], args.get("mode", "overwrite")), False)
    if name == "send_email":
        return (send_email(args["to"], args["subject"], args["body"], args.get("cc", "")), False)
    if name == "browser_navigate":
        return (browser_navigate(args["url"]), True)
    if name == "browser_list_elements":
        return (browser_list_elements(), False)
    if name == "browser_click_element_index":
        return (browser_click_element_index(args["index"]), False)
    if name == "browser_click_grid_cell":
        return (
            browser_click_grid_cell(
                args["container_index"], args["row"], args["col"], args["rows"], args["cols"]
            ),
            False,
        )
    if name == "browser_get_text":
        return (browser_get_text(), False)
    if name == "desktop_list_elements":
        return (desktop_list_elements(args.get("scope", "foreground")), False)
    if name == "desktop_click_element_index":
        return (desktop_click_element_index(args["index"], args.get("scope", "foreground")), False)
    if name == "move_and_click":
        real_x, real_y = human_move_and_click(
            args["x"], args["y"], args.get("button", "left"), args.get("target_description", "")
        )
        return (
            f"Clicked at image coords ({args['x']}, {args['y']}) -> screen coords ({real_x}, {real_y}) "
            f"with {args.get('button', 'left')} button.",
            False,
        )
    if name == "type_text":
        human_type(args["text"])
        return (f"Typed: {args['text']!r}", False)
    if name == "press_key":
        keys = args["keys"]
        pyautogui.hotkey(*keys.split("+"))
        return (f"Pressed keys: {keys}", keys.strip().lower() in {"enter", "return"})
    if name == "scroll":
        pyautogui.scroll(args["amount"])
        return (f"Scrolled {args['amount']}", False)
    if name == "wait":
        time.sleep(args["seconds"])
        return (f"Waited {args['seconds']}s", False)
    if name == "retry_last_action":
        return (f"Retrying with correction: {args.get('corrected_action', '')}", True)
    if name == "done":
        return (f"DONE: {args.get('summary', '')}", False)
    return (f"Unknown action: {name}", False)


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------
def run_agent(task: str):
    screen_w, screen_h = pyautogui.size()
    raw_screenshot = pyautogui.screenshot()
    if raw_screenshot.size != (screen_w, screen_h):
        print(
            f"[warning] pyautogui.size() reports {screen_w}x{screen_h} but the raw screenshot is "
            f"{raw_screenshot.size[0]}x{raw_screenshot.size[1]}. This mismatch (common with "
            "multi-monitor setups or unusual display scaling) will especially hurt accuracy near "
            "screen edges. If you have multiple monitors, consider running the target app on the "
            "primary monitor only, or investigate pyautogui's multi-monitor support for your setup."
        )
    system_prompt = (
        "You are an autonomous Windows desktop agent running with full permissions on the "
        "user's own machine — no confirmations are required, act directly. "
        f"The user's home directory is: {USER_HOME}\n\n"

        "## How to handle ANY task, including vague/high-level ones\n"
        "The user will often give you a short, high-level goal (e.g. 'play tic-tac-toe', "
        "'tu es comptable, fais le job') instead of precise step-by-step instructions. You will "
        "NEVER get clarification mid-task — there is no human in this loop. So: silently break "
        "the goal down yourself into an ordered plan of concrete sub-goals in your `reasoning`, "
        "and execute it step by step, revising the plan as you learn new information. Do not "
        "wait for a precise instruction that will never come — decide a reasonable approach and "
        "commit to it.\n\n"
        "General pattern for any 'do X' goal:\n"
        "  1. CHECK current state before assuming anything (is the app already installed/open? "
        "does the file already exist? what's actually on screen right now?).\n"
        "  2. If the direct path isn't available, don't stop — pivot to an alternative "
        "automatically (see fallback chain below) instead of reporting failure.\n"
        "  3. Only call `done` once the actual goal is achieved, not just the setup step "
        "(e.g. for 'play tic-tac-toe', installing/opening something is not done — you must "
        "actually play: read the board from the screenshot, decide a move, click it, repeat "
        "until the game ends).\n\n"
        "Example fallback chain for 'play/use app X':\n"
        "  a) `run_shell_command('where X')` or `tasklist` to see if it's already installed/running.\n"
        "  b) If not found, try installing it silently: "
        "`run_shell_command('winget install X.X -e --silent --accept-package-agreements "
        "--accept-source-agreements')` (search first with `winget search X` if the exact ID is "
        "unclear) — this is far more reliable than clicking through the Microsoft Store GUI.\n"
        "  c) If there's no relevant installable app, or a web version is simpler/faster, use "
        "`browser_navigate(url)` to open it — this gives you exact, element-based clicking (see "
        "the browser_* tools below), not pixel-guessing.\n"
        "  d) For a native desktop app with no web/CLI equivalent, switch to GUI tools (see "
        "below) to interact with it once open.\n\n"

        "## Tool preferences\n"
        "STRONGLY PREFER text tools over GUI clicking whenever a non-visual path exists:\n"
        "- To open an app: use `open_application` (or winget/start as above), never click a "
        "desktop icon.\n"
        "- To read/write/move files: `run_shell_command`, `list_directory`, `read_text_file`, "
        "`write_text_file` — never navigate File Explorer by clicking.\n"
        "- To send an email: `send_email` directly.\n\n"
        "## ANY website interaction: use the browser_* tools, never pixel-click\n"
        "`move_and_click` is a rough vision-based pixel guess and is NOT reliable for precise "
        "targets (small buttons, grid cells, game boards) — do not use it for websites, ever. "
        "For any website:\n"
        "  1. `browser_navigate(url)` opens it in a real controllable browser.\n"
        "  2. `browser_list_elements()` lists the actual clickable elements with their index.\n"
        "  3. `browser_click_element_index(index)` clicks one exactly — no coordinate guessing.\n"
        "  4. For grids (e.g. a 3x3 tic-tac-toe board, a canvas game board): "
        "`browser_click_grid_cell(container_index, row, col, rows, cols)` computes the exact "
        "pixel from the element's real bounding box — this is the correct way to play any "
        "grid-based web game, do not try to eyeball individual cells with move_and_click.\n"
        "  5. `browser_get_text()` reads the page's actual text to confirm state (e.g. whose "
        "turn it is, if a move registered) instead of guessing from a screenshot.\n"
        "Only fall back to `move_and_click` on a webpage if `browser_list_elements` genuinely "
        "shows nothing usable for the target (rare).\n\n"
        "## Native apps, desktop icons, Start menu: use desktop_* tools, not pixel-click\n"
        "Same principle as the browser tools, applied to Windows itself: `desktop_list_elements` "
        "reads the real OS accessibility tree (scope='foreground' for the active app's buttons/"
        "menus/controls, scope='desktop' for desktop icons) and gives you exact elements with an "
        "index. `desktop_click_element_index(index, scope)` clicks the real element — no pixel "
        "guessing. Use this for: clicking buttons/menus/checkboxes in any native Windows app, "
        "clicking desktop icons, navigating the Start menu once open. Try this BEFORE "
        "move_and_click whenever you're about to click something in a native app or on the "
        "desktop.\n\n"
        "`move_and_click` (vision-based pixel guessing) is the LAST resort, only for genuinely "
        "custom-drawn content with no accessibility tree — e.g. a canvas-based game, a custom "
        "Electron UI, or content inside a screenshot where desktop_list_elements/"
        "browser_list_elements found nothing relevant. It is inherently less reliable than the "
        "tools above, since it relies on the vision model guessing coordinates rather than "
        "reading real element positions — expect it to sometimes miss, and re-verify after using "
        "it.\n\n"
        "`type_text`, `press_key`, and `scroll` remain normal, reliable tools (typing/keys/"
        "scrolling don't require guessing a position) — it's specifically `move_and_click` that "
        "is the fallback.\n\n"

        "## Verification discipline\n"
        "Every tool call requires `observation` (what you actually know right now — from command "
        "output/exit codes or the screenshot — and whether the previous action really worked) "
        "and `reasoning`. Never assume success just because you issued an action. If something "
        "failed, say so plainly and correct course (different command, different app, wait "
        "longer, retry) instead of proceeding as if it worked or giving up silently. Some things "
        "(page loads, app startup, installs) take time — use `wait` before concluding failure. "
        "Only call `done` when the actual underlying goal is verified complete, not just a setup "
        "step along the way.\n\n"
        "Each screenshot is accompanied by a 'Ground-truth mouse cursor position' line — this is "
        "the real OS-reported cursor location, not something you have to visually estimate. Use "
        "it as a reliable anchor: e.g. if you just tried to click something and the cursor ended "
        "up somewhere unexpected relative to it, that confirms a miss even before the next "
        "screenshot changes.\n\n"
        "If a click doesn't register (board/UI unchanged after clicking), do NOT repeat the exact "
        "same (x, y) — that will just fail the same way again. Instead, look carefully at the "
        "current screenshot again and give a meaningfully different, more careful coordinate "
        "estimate (the target's actual on-screen position may simply have been misjudged)."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Task: {task}"},
    ]

    consecutive_failures = 0

    for step in range(1, MAX_STEPS + 1):
        # Always take a fresh screenshot every step. Skipping this after
        # text/browser/desktop tool calls previously meant that if the model
        # then chose move_and_click, it would be clicking based on a stale,
        # possibly many-steps-old screenshot — a real source of wrong clicks.
        # Token cost is not a constraint here, so always look before deciding.
        screenshot_b64, img_w, img_h = take_screenshot_b64()
        mouse_info = get_mouse_ground_truth_text()
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Step {step}. Current screen (image is {img_w}x{img_h}px — a red "
                            f"grid every {GRID_STEP}px with axis labels is overlaid to help you "
                            "estimate click coordinates; give coordinates relative to THIS "
                            f"image, they are auto-scaled to the real screen).\n{mouse_info}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                ],
            }
        )

        response = call_chat_completion_with_retry(
            model=MODEL, messages=messages, tools=TOOLS, tool_choice="required",
        )
        msg = response.choices[0].message
        if not msg.tool_calls:
            print("Model returned no tool call, stopping.")
            break

        call = msg.tool_calls[0]
        name = call.function.name
        args = json.loads(call.function.arguments or "{}")

        print(f"[step {step}] observation: {args.get('observation', '')}")
        print(f"[step {step}] reasoning:   {args.get('reasoning', '')}")
        shown_args = {k: v for k, v in args.items() if k not in ("observation", "reasoning")}
        print(f"[step {step}] action:      {name}({shown_args})")

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [{"id": call.id, "type": "function",
                                "function": {"name": name, "arguments": call.function.arguments}}],
            }
        )

        result_text, needs_settle = execute_action(name, args)
        consecutive_failures = consecutive_failures + 1 if name == "retry_last_action" else 0
        messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})
        print(f"[step {step}] result:      {result_text[:300]}")

        if name == "done":
            print(result_text)
            break

        # (screenshot is now always taken at the top of the loop; needs_settle
        # still controls how long we wait before that next screenshot below)

        delay = BASE_STEP_DELAY
        if needs_settle:
            delay = NAV_SETTLE_DELAY
        if consecutive_failures > 1:
            escalation = min(consecutive_failures - 1, MAX_CONSECUTIVE_FAILURES) * FAILURE_ESCALATION_STEP
            delay += escalation
            print(f"[step {step}] consecutive failures: {consecutive_failures} -> +{escalation:.1f}s")
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            messages.append(
                {"role": "user", "content": (
                    f"Retry attempt #{consecutive_failures} on the same step. If waiting hasn't "
                    "helped, try a genuinely different approach (different command, reopen the "
                    "app, different file path, etc.)."
                )}
            )

        time.sleep(delay)
    else:
        print("Max steps reached, stopping.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python agent_controller.py "your task description"')
        sys.exit(1)
    run_agent(" ".join(sys.argv[1:]))
