from __future__ import annotations

import base64
import re
import time
import threading
from io import BytesIO
from typing import Any

import pyautogui
import requests
from PIL import Image, ImageGrab
from flask import Flask, request, jsonify
from flask_cors import CORS

try:
    import pygetwindow as gw
    _HAS_WINDOW_API = True
except Exception:
    gw = None
    _HAS_WINDOW_API = False

app = Flask(__name__)
CORS(app)

BASE_URL      = "http://localhost:8000/v1"
MAX_STEPS     = 20
PROFILE       = "2b"
MODEL_PROFILES = {
    "2b":     {"model": "ui-tars-2b",     "coord_space": "normalized_1000"},
    "7b-sft": {"model": "ui-tars-7b",     "coord_space": "normalized_1000"},
    "7b-dpo": {"model": "ui-tars-7b",     "coord_space": "normalized_1000"},
    "1.5-7b": {"model": "ui-tars-1.5-7b", "coord_space": "absolute"},
}
MODEL         = MODEL_PROFILES[PROFILE]["model"]
COORD_SPACE   = MODEL_PROFILES[PROFILE]["coord_space"]
MAX_LONG_SIDE = 1280

# ── Prompts ───────────────────────────────────────────────────────────────────

# Generic fallback agent — used only for instructions that AREN'T a plain
# "open <app>" launch (e.g. "Browse YouTube"), and for follow-up actions
# after a launch has already been confirmed.
PROMPT_AGENT = """You are a GUI agent on Windows 11.
Goal: {goal}

Actions taken so far:
{history}

Look at the current screenshot and output EXACTLY two lines:
Thought: <what you see right now and what the next step is>
Action: <one action>

ACTIONS:
hotkey(key='win')                                              ← open Start menu
type(content='<text>')                                         ← type text; add \\n to press Enter
click(start_box='<|box_start|>(x1,y1)<|box_end|>')            ← click element
left_double(start_box='<|box_start|>(x1,y1)<|box_end|>')      ← double-click element
hotkey(key='ctrl+t')                                           ← open new browser tab
hotkey(key='ctrl+l')                                           ← focus browser address bar
scroll(start_box='<|box_start|>(x1,y1)<|box_end|>', direction='down')
wait()                                                         ← wait for screen to update
finished()                                                     ← ONLY when goal is fully complete

RULES:
- Decide based ONLY on what you see in the screenshot.
- If the app is already open and visible → call finished() immediately.
- If app is open but minimized → click its taskbar icon to restore it.
- Never repeat an action already in the history.
- Never close windows, never use alt+f4 or ctrl+w.
- ONE action only."""

# Used for follow-up actions once we've already confirmed the target app
# is open and in the foreground (via _run_launch_flow). Without this, the
# model reuses the generic prompt, sees "chrome" in the goal text, and
# repeatedly tries to "open" an app that's already the active window —
# clicking its taskbar icon over and over, which just minimizes/restores
# it instead of doing anything.
PROMPT_FOLLOWUP = """You are a GUI agent on Windows 11.
The application "{app}" is ALREADY OPEN and is the ACTIVE, FOREGROUND
window right now. Do NOT try to open, launch, restore, or click any
taskbar icon for it — that would only minimize/restore a window that's
already in front of you and accomplishes nothing.

Remaining goal, to be done INSIDE this already-open app: {goal}

Actions taken so far:
{history}

Look at the current screenshot and output EXACTLY two lines:
Thought: <what you see right now and what the next step is>
Action: <one action>

ACTIONS:
type(content='<text>')                                         ← type text; add \\n to press Enter
click(start_box='<|box_start|>(x1,y1)<|box_end|>')            ← click element
left_double(start_box='<|box_start|>(x1,y1)<|box_end|>')      ← double-click element
hotkey(key='ctrl+t')                                           ← open a new browser tab
hotkey(key='ctrl+l')                                           ← focus the browser address bar
scroll(start_box='<|box_start|>(x1,y1)<|box_end|>', direction='down')
wait()                                                         ← wait for screen to update
finished()                                                     ← ONLY when goal is fully complete

RULES:
- The app is ALREADY OPEN AND FOCUSED. NEVER click a taskbar icon.
- You MUST NOT call type() unless you can see a text cursor / focused
  input field in the screenshot right now. If you don't see one yet,
  your action this turn must be a click or hotkey to create one first —
  never a blind type().
- Decide based ONLY on what you see in the screenshot.
- Never repeat an action already in the history.
- Never close windows, never use alt+f4 or ctrl+w.
- ONE action only.

EXAMPLE — goal "search argentina vs egypt" inside an already-open browser,
no input field focused yet:
  Turn 1:
    Thought: The browser is open but no field is focused, so I can't type
    yet. I'll focus the address bar first.
    Action: hotkey(key='ctrl+l')
  Turn 2 (after the address bar is now visibly focused/highlighted):
    Thought: The address bar is focused. I'll type the search and press Enter.
    Action: type(content='argentina vs egypt\\n')
  Turn 3:
    Thought: The search results are visible. Goal complete.
    Action: finished()

The same two-step pattern (focus first, then type) applies to any app —
if it's not a browser, click directly on the visible input/search field
instead of using ctrl+l."""

# Narrow perception prompt: asks the model to classify state only,
# nothing else. Much more reliable for a 2B model than open-ended planning.
CHECK_STATE_PROMPT = """You are looking at a Windows 11 screenshot.
Look at the taskbar at the bottom of the screen, and at whatever window
is currently in the foreground.

Question: is the application "{app}" currently running?

Reply with EXACTLY one line:
STATE: OPEN_FOREGROUND
or
STATE: OPEN_BACKGROUND
or
STATE: CLOSED

- OPEN_FOREGROUND = "{app}" window is visible and active right now.
- OPEN_BACKGROUND = "{app}" has a taskbar icon (running) but is not the
  visible/focused window.
- CLOSED = no evidence "{app}" is running anywhere."""

# Coordinate-finding prompt, used only when we already know the app is
# running in the background and need to click its taskbar icon.
FIND_TASKBAR_ICON_PROMPT = """Look at the taskbar icons at the bottom of
this Windows 11 screenshot. Find the icon for the running application
"{app}" and output EXACTLY one line:
Action: click(start_box='<|box_start|>(x,y)<|box_end|>')"""

# ─────────────────────────────────────────────────────────────────────────────

LAUNCH_RE = re.compile(r"^\s*(?:open|launch|start)\s+(.+)$", re.I)

# Generic matcher — no per-app knowledge required. First tries an exact/
# substring match, then falls back to shared significant words (so
# "microsoft edge" matches a title like "New tab - Microsoft Edge" without
# us ever having listed "edge" anywhere). Anything this misses falls
# through to CLOSED and gets resolved by the vision-grounded check later
# in the flow, rather than a blind text guess here.
_STOPWORDS = {"the", "a", "an", "app", "application", "open", "launch",
              "start", "and", "for", "new", "window"}


def _title_matches(app_name: str, title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    a = app_name.lower().strip()
    if a in t or t in a:
        return True
    a_words = {w for w in re.findall(r"[a-z0-9]+", a)
               if len(w) > 2 and w not in _STOPWORDS}
    t_words = set(re.findall(r"[a-z0-9]+", t))
    return bool(a_words & t_words)

_task_state: dict[str, Any] = {"running": False, "log": [], "status": "idle"}
_task_lock  = threading.Lock()


def _log(msg: str) -> None:
    with _task_lock:
        _task_state["log"].append(msg)


def capture_screen() -> tuple[str, tuple[int, int]]:
    image = ImageGrab.grab().convert("RGB")
    w, h  = image.size
    long_side = max(w, h)
    if long_side > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / long_side
        w, h  = round(w * scale), round(h * scale)
        image = image.resize((w, h), Image.LANCZOS)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), (w, h)


def ask_model(prompt: str, image_url: str) -> str:
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": [
            {"type": "text",      "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages,
              "temperature": 0, "max_tokens": 128, "frequency_penalty": 0},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def ask_model_text(prompt: str) -> str:
    """Text-only call — no screenshot. Used for cheap semantic reasoning
    (like matching an app name to a window title) that doesn't need
    vision, so we're not spending a screenshot round-trip on it."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": prompt},
    ]
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages,
              "temperature": 0, "max_tokens": 64, "frequency_penalty": 0},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def parse_action(raw: str) -> str:
    for line in raw.splitlines():
        s = line.strip()
        if s.lower().startswith("action:"):
            return s[len("action:"):].strip()
    verb = re.compile(
        r"^(click|left_double|right_single|drag|hotkey|type|scroll|wait|finished|stop)\b",
        re.I,
    )
    for line in raw.splitlines():
        s = line.strip()
        if verb.match(s):
            return s
    return raw.strip()


def parse_thought(raw: str) -> str:
    for line in raw.splitlines():
        s = line.strip()
        if s.lower().startswith("thought:"):
            return s[len("thought:"):].strip()
    return ""


def parse_state(raw: str) -> str:
    m = re.search(r"STATE:\s*(OPEN_FOREGROUND|OPEN_BACKGROUND|CLOSED)", raw, re.I)
    return m.group(1).upper() if m else "CLOSED"


def normalize_key(key: str) -> str:
    return {
        "win": "winleft", "windows": "winleft", "cmd": "winleft",
        "meta": "winleft", "super": "winleft",
        "return": "enter", "esc": "escape", "del": "delete",
        "control": "ctrl",
    }.get(key.strip().lower(), key.strip().lower())


def to_screen_xy(x: float, y: float, img_size: tuple[int, int]) -> tuple[int, int]:
    sw, sh = pyautogui.size()
    if COORD_SPACE == "normalized_1000":
        return (round(sw * max(0.0, min(1000.0, x)) / 1000),
                round(sh * max(0.0, min(1000.0, y)) / 1000))
    iw, ih = img_size
    return round(x * sw / iw), round(y * sh / ih)


def coords_in(action: str) -> list[float]:
    inner = action[action.find("(") + 1: action.rfind(")")] if "(" in action else action
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", inner)]


def point_from(action: str, img_size: tuple[int, int],
               box_key: str = "start_box") -> tuple[int, int]:
    seg = action
    if box_key in action:
        seg = action.split(box_key, 1)[1]
        end = seg.find(")")
        seg = seg[: end + 1] if end != -1 else seg
    nums = coords_in(seg)
    if len(nums) >= 4:
        return to_screen_xy((nums[0] + nums[2]) / 2,
                            (nums[1] + nums[3]) / 2, img_size)
    if len(nums) >= 2:
        return to_screen_xy(nums[0], nums[1], img_size)
    raise ValueError(f"No coordinates in: {action}")


_FORBIDDEN = [
    re.compile(r"alt\s*\+\s*f4", re.I),
    re.compile(r"ctrl\s*\+\s*w\b", re.I),
]


def execute_action(action: str, img_size: tuple[int, int]) -> tuple[bool, bool]:
    """Execute action. Returns (keep_going, submitted).
    keep_going is False when the agent should stop (finished/stop).
    submitted is True when this action just pressed Enter to submit
    something — either type(...\\n) or an explicit Enter hotkey — which
    callers can use as a hard signal that a search/entry was completed,
    rather than waiting for the model to notice on its own."""
    if any(p.search(action) for p in _FORBIDDEN):
        _log(f"🚫 blocked: {action}")
        return True, False

    lower = action.lower().strip()

    if lower.startswith(("finished", "stop", "call_user")):
        return False, False

    if lower.startswith("wait"):
        _log("⏳ waiting...")
        time.sleep(2)
        return True, False

    if lower.startswith("hotkey"):
        m = re.search(r"keys?\s*=\s*['\"](.+?)['\"]", action, re.I)
        if not m:
            raise ValueError(f"Bad hotkey: {action}")
        keys = [normalize_key(k) for k in re.split(r"[+\s]+", m.group(1).strip()) if k]
        _log(f"⌨ hotkey: {'+'.join(keys)}")
        pyautogui.press(keys[0]) if len(keys) == 1 else pyautogui.hotkey(*keys)
        submitted = any(k in ("enter", "return") for k in keys)
        return True, submitted

    if lower.startswith("type"):
        m = re.search(r"content\s*=\s*['\"](.+?)['\"]\s*\)", action, re.I | re.S)
        if not m:
            raise ValueError(f"Bad type: {action}")
        text = m.group(1)
        try:
            text = text.encode("utf-8").decode("unicode_escape")
        except Exception:
            pass
        submit = text.endswith("\n")
        text   = text.rstrip("\n")
        _log(f"⌨ type: {text!r}{'+ Enter' if submit else ''}")
        time.sleep(0.3)
        pyautogui.write(text, interval=0.04)
        if submit:
            time.sleep(0.2)
            pyautogui.press("enter")
        return True, submit

    if lower.startswith("scroll"):
        m         = re.search(r"direction\s*=\s*['\"](\w+)", action, re.I)
        direction = m.group(1).lower() if m else "down"
        _log(f"🖱 scroll {direction}")
        try:
            x, y = point_from(action, img_size)
            pyautogui.moveTo(x, y)
        except ValueError:
            pass
        pyautogui.scroll(300 if direction in ("up", "left") else -300)
        return True, False

    if lower.startswith("drag"):
        sx, sy = point_from(action, img_size, "start_box")
        ex, ey = point_from(action, img_size, "end_box")
        _log(f"🖱 drag ({sx},{sy}) → ({ex},{ey})")
        pyautogui.moveTo(sx, sy)
        pyautogui.dragTo(ex, ey, duration=0.4, button="left")
        return True, False

    if lower.startswith("left_double"):
        x, y = point_from(action, img_size)
        _log(f"🖱 double-click ({x},{y})")
        pyautogui.doubleClick(x, y)
        return True, False

    if lower.startswith("right_single"):
        x, y = point_from(action, img_size)
        _log(f"🖱 right-click ({x},{y})")
        pyautogui.click(x, y, button="right")
        return True, False

    if lower.startswith("click"):
        x, y = point_from(action, img_size)
        _log(f"🖱 click ({x},{y})")
        pyautogui.click(x, y)
        return True, False

    raise ValueError(f"Unknown action: {action}")


def estimate_steps(instruction: str) -> int:
    """
    Return a tight step budget based on task complexity.
    - Just open an app          → 4 steps  (check → win key → type+enter → confirm)
    - Open + one action         → 7 steps  (above + 3 for the follow-up)
    - Open + multi-step task    → 12 steps
    - Fallback to user MAX_STEPS if nothing matches
    """
    text = instruction.lower()
    has_followup = bool(re.search(r"\b(and|then|search|type|write|go to|navigate|open tab)\b", text))
    if not has_followup:
        return 4
    # count distinct sub-tasks separated by "and/then"
    parts = re.split(r"\s+(?:and|then)\s+", text)
    if len(parts) <= 2:
        return 7
    return min(12, MAX_STEPS)


# ── Perception helper ────────────────────────────────────────────────────────
# State detection uses the Windows window API as the primary source of
# truth — it's instant and unambiguous, unlike asking a 2B VLM to eyeball
# tiny taskbar icons. If nothing matches by wording, we deliberately do
# NOT ask a text-only model to guess a semantic match here — a 2B model
# with no image to look at will occasionally hallucinate a match instead
# of correctly saying "none", and a false "it's already open" is worse
# than a false "it's closed" (the former silently skips launching the
# app; the latter just launches it, harmless even if already running).
# Genuinely ambiguous cases get resolved later, with actual pixels in
# front of the model, via the vision-based check in _search_and_open.

def _native_check_state(app_name: str):
    """Returns (state, window) using the OS window list, or None if the
    window API isn't available."""
    if not _HAS_WINDOW_API:
        return None
    try:
        active = gw.getActiveWindow()
    except Exception:
        active = None
    active_title = (active.title if active else "") or ""

    try:
        windows = [w for w in gw.getAllWindows() if w.title and w.title.strip()]
    except Exception:
        windows = []

    if active is not None and _title_matches(app_name, active_title):
        return "OPEN_FOREGROUND", active
    for w in windows:
        if _title_matches(app_name, w.title):
            return "OPEN_BACKGROUND", w

    return "CLOSED", None


def _vision_check_state(app_name: str):
    """Original screenshot + narrow classification fallback."""
    url, _ = capture_screen()
    _log(f"📸 checking whether '{app_name}' is open (vision)...")
    raw = ask_model(CHECK_STATE_PROMPT.format(app=app_name), url)
    state = parse_state(raw)
    _log(f"🔎 state: {state}")
    return state, None


def _check_state(app_name: str):
    native = _native_check_state(app_name)
    if native is not None:
        state, win = native
        _log(f"🔎 state (window check): {state}")
        return state, win
    return _vision_check_state(app_name)


def _bring_to_foreground(app_name: str, win=None, max_tries: int = 2) -> bool:
    # Prefer activating the window directly via the OS — no clicking,
    # no coordinate guessing, no risk of missing a small taskbar icon.
    if win is not None:
        try:
            if win.isMinimized:
                win.restore()
            win.activate()
            time.sleep(0.6)
            _log(f"🪟 brought '{app_name}' to foreground (window API)")
            return True
        except Exception as exc:
            _log(f"[warn] window activate failed ({exc}), falling back to click")

    for attempt in range(1, max_tries + 1):
        url, img_size = capture_screen()
        _log(f"📸 locating '{app_name}' in the taskbar (try {attempt}/{max_tries})...")
        raw = ask_model(FIND_TASKBAR_ICON_PROMPT.format(app=app_name), url)
        action = parse_action(raw)
        try:
            x, y = point_from(action, img_size)
        except ValueError as exc:
            _log(f"[error] couldn't locate taskbar icon: {exc}")
            return False
        _log(f"🖱 click taskbar icon ({x},{y})")
        pyautogui.click(x, y)
        time.sleep(1.0)
        state, _ = _check_state(app_name)
        if state == "OPEN_FOREGROUND":
            _log(f"✅ '{app_name}' brought to foreground.")
            return True
    _log(f"⚠ could not bring '{app_name}' to foreground.")
    return False


def _search_and_open(app_name: str, max_confirm_tries: int = 8,
                      confirm_wait: float = 1.0) -> bool:
    _log("⌨ opening Start menu...")
    pyautogui.press("winleft")
    time.sleep(0.6)

    _log(f"⌨ typing '{app_name}'...")
    pyautogui.write(app_name, interval=0.04)
    time.sleep(0.4)
    pyautogui.press("enter")

    _log("⏳ waiting for app to launch...")
    time.sleep(confirm_wait)

    for attempt in range(1, max_confirm_tries + 1):
        state, win = _check_state(app_name)
        if state == "OPEN_FOREGROUND":
            _log(f"✅ '{app_name}' opened successfully.")
            return True
        if state == "OPEN_BACKGROUND":
            return _bring_to_foreground(app_name, win)
        _log(f"⏳ not open yet — retry {attempt}/{max_confirm_tries}...")
        time.sleep(confirm_wait)

    # Native check exhausted its retries. Before giving up, try once with
    # vision — it can catch cases the window-title alias table doesn't
    # cover — and log what windows actually exist, so a real mismatch
    # (missing alias, unusual title) is visible instead of a silent fail.
    if _HAS_WINDOW_API:
        try:
            titles = [t for t in gw.getAllTitles() if t.strip()]
        except Exception:
            titles = []
        _log(f"🪟 open windows seen: {titles or '(none)'}")
        _log("📸 native check exhausted — trying one vision-based check...")
        state, _ = _vision_check_state(app_name)
        if state == "OPEN_FOREGROUND":
            _log(f"✅ '{app_name}' opened successfully (confirmed via vision).")
            return True

    _log(f"❌ '{app_name}' did not open after {max_confirm_tries} checks.")
    return False


# ── Generic fallback loop ────────────────────────────────────────────────────
# Used for instructions that aren't a plain "open <app>" (e.g. "Browse
# YouTube"), and for follow-up actions once a launch is already confirmed.

def _action_kind(action: str) -> str:
    m = re.match(r"^(\w+)", action.strip())
    return m.group(1).lower() if m else action.strip().lower()


def _is_single_search(instruction: str) -> bool:
    """True for a plain 'search ...' follow-up — once the query is
    actually submitted (Enter pressed), there's nothing left to do. We
    don't rely on the model noticing that itself; a small VLM will
    happily keep re-searching the same thing turn after turn."""
    return bool(re.match(r"^\s*search\b", instruction, re.I))


def _run_generic_flow(instruction: str, step_budget: int | None = None,
                       already_open_app: str | None = None) -> bool:
    step_budget = step_budget or estimate_steps(instruction)
    _log(f"🔢 Step budget: {step_budget}")

    single_search = _is_single_search(instruction)

    action_history: list[str] = []
    kind_history: list[str] = []
    last_action = ""
    same_count  = 0
    finished_ok = False

    for step in range(1, step_budget + 1):
        url, img_size = capture_screen()
        _log(f"📸 Step {step}/{step_budget} — screenshot taken")

        history = "\n".join(
            f"  {i+1}. {a}" for i, a in enumerate(action_history)
        ) or "  (none yet)"

        if already_open_app:
            prompt = PROMPT_FOLLOWUP.format(app=already_open_app,
                                             goal=instruction, history=history)
        else:
            prompt = PROMPT_AGENT.format(goal=instruction, history=history)

        try:
            raw = ask_model(prompt, url)
        except Exception as exc:
            _log(f"[error] model: {exc}")
            break

        action  = parse_action(raw)
        thought = parse_thought(raw)
        if thought:
            _log(f"💭 {thought}")
        _log(f"▶ {action}")

        if action.lower().startswith(("finished", "stop")):
            _log("✅ Agent confirmed task complete.")
            finished_ok = True
            break

        if action == last_action:
            same_count += 1
        else:
            same_count  = 0
            last_action = action

        # A repeated wait() is legitimate while something loads — don't
        # treat it as "stuck".
        if same_count >= 2 and not action.lower().startswith("wait"):
            _log("⚠ stuck — stopping")
            break

        # Exact-match repeats aren't the only sign of a stuck loop — a
        # model clicking roughly the same spot with drifting coordinates
        # each time (e.g. a taskbar icon) never trips the check above but
        # is just as stuck. Catch 3 consecutive clicks of the same kind.
        kind = _action_kind(action)
        kind_history.append(kind)
        if (len(kind_history) >= 3 and len(set(kind_history[-3:])) == 1
                and kind in ("click", "left_double")):
            _log("⚠ repeated clicking without progress — stopping")
            break

        action_history.append(action)

        try:
            keep_going, submitted = execute_action(action, img_size)
        except Exception as exc:
            _log(f"[error] execute: {exc}")
            continue

        if not keep_going:
            _log("✅ Agent confirmed task complete.")
            finished_ok = True
            break

        if single_search and submitted:
            _log("✅ search submitted — goal complete, stopping here "
                 "(not waiting for the model to notice).")
            finished_ok = True
            break

        time.sleep(1.5)

    return finished_ok


# ── Launch flow: CHECK → SEARCH/OPEN → CONFIRM ──────────────────────────────
# This is the deterministic state machine driving "open <app>" instructions.
# The model is only ever asked narrow perception questions here; the code
# decides which phase to run next.

def _run_launch_flow(app_name: str, followup: str | None) -> None:
    state, win = _check_state(app_name)

    if state == "OPEN_FOREGROUND":
        _log(f"✅ '{app_name}' is already open and in the foreground.")
        opened = True
    elif state == "OPEN_BACKGROUND":
        opened = _bring_to_foreground(app_name, win)
    else:
        opened = _search_and_open(app_name)

    if not opened:
        _log("⚠ Task ended — could not confirm the app opened.")
        with _task_lock:
            _task_state["status"] = "error"
            _task_state["running"] = False
        return

    if followup:
        _log(f"➡ continuing with follow-up: {followup}")
        finished_ok = _run_generic_flow(followup, step_budget=8,
                                         already_open_app=app_name)
        _log("✅ Task complete!" if finished_ok else "⚠ Follow-up ended without explicit confirmation.")
        with _task_lock:
            _task_state["status"] = "success" if finished_ok else "done"
            _task_state["running"] = False
        return

    _log("✅ Task complete!")
    with _task_lock:
        _task_state["status"] = "success"
        _task_state["running"] = False


# ── Entry point ──────────────────────────────────────────────────────────────

def run_agent(instruction: str) -> None:
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE    = 0.25

    with _task_lock:
        _task_state.update({"running": True, "log": [], "status": "running"})

    _log(f"📋 Task: {instruction}")

    m = LAUNCH_RE.match(instruction.strip())
    if m:
        # "open <app>" or "open <app> and/then <followup>"
        app_and_rest = m.group(1).strip()
        parts = re.split(r"\s+(?:and|then)\s+", app_and_rest, maxsplit=1)
        app_name = parts[0].strip()
        followup = parts[1].strip() if len(parts) > 1 else None
        try:
            _run_launch_flow(app_name, followup)
        except Exception as exc:
            _log(f"[error] {exc}")
            with _task_lock:
                _task_state["status"] = "error"
                _task_state["running"] = False
        return

    # Not a plain "open X" instruction — fall back to the generic loop.
    try:
        finished_ok = _run_generic_flow(instruction)
    except Exception as exc:
        _log(f"[error] {exc}")
        with _task_lock:
            _task_state["status"] = "error"
            _task_state["running"] = False
        return

    _log("✅ Task complete!" if finished_ok else "⚠ Task ended without explicit confirmation.")
    with _task_lock:
        _task_state["status"] = "success" if finished_ok else "done"
        _task_state["running"] = False


# ── Flask ─────────────────────────────────────────────────────────────────────

@app.route("/config", methods=["GET"])
def get_config():
    return jsonify({"max_steps": MAX_STEPS, "model": MODEL,
                    "max_long_side": MAX_LONG_SIDE,
                    "profiles": list(MODEL_PROFILES.keys())})


@app.route("/config", methods=["POST"])
def set_config():
    data = request.get_json() or {}
    global MAX_STEPS, MODEL, COORD_SPACE, MAX_LONG_SIDE
    if "max_steps" in data:
        MAX_STEPS = int(data["max_steps"])
    if "profile" in data and data["profile"] in MODEL_PROFILES:
        profile     = data["profile"]
        MODEL       = MODEL_PROFILES[profile]["model"]
        COORD_SPACE = MODEL_PROFILES[profile]["coord_space"]
    if "max_long_side" in data:
        MAX_LONG_SIDE = int(data["max_long_side"])
    return jsonify({"max_steps": MAX_STEPS, "model": MODEL,
                    "max_long_side": MAX_LONG_SIDE})


@app.route("/run", methods=["POST"])
def run():
    data        = request.get_json()
    instruction = (data or {}).get("app", "").strip()
    if not instruction:
        return jsonify({"error": "instruction is required"}), 400
    with _task_lock:
        if _task_state["running"]:
            return jsonify({"error": "agent already running"}), 409
    threading.Thread(target=run_agent, args=(instruction,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/status", methods=["GET"])
def status():
    with _task_lock:
        return jsonify({
            "running": _task_state["running"],
            "status":  _task_state["status"],
            "log":     list(_task_state["log"]),
        })


if __name__ == "__main__":
    app.run(port=5000, debug=False)