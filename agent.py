from __future__ import annotations

import base64
import ctypes
import platform
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

app = Flask(__name__)
CORS(app)

BASE_URL = "http://localhost:8000/v1"
MAX_STEPS = 15
PROFILE = "2b"
MODEL_PROFILES = {
    "2b":     {"model": "ui-tars-2b",     "coord_space": "normalized_1000"},
    "7b-sft": {"model": "ui-tars-7b",     "coord_space": "normalized_1000"},
    "7b-dpo": {"model": "ui-tars-7b",     "coord_space": "normalized_1000"},
    "1.5-7b": {"model": "ui-tars-1.5-7b", "coord_space": "absolute"},
}
MODEL = MODEL_PROFILES[PROFILE]["model"]
DRY_RUN = False
COORD_SPACE = MODEL_PROFILES[PROFILE]["coord_space"]
MAX_IMAGES = 1
MAX_LONG_SIDE = 1280
LANGUAGE = "English"

COMPUTER_USE = """You are a GUI agent operating a Windows 11 computer. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
Thought: ...
Action: ...

## Action Space
click(start_box='<|box_start|>(x1,y1)<|box_end|>')
left_double(start_box='<|box_start|>(x1,y1)<|box_end|>')
right_single(start_box='<|box_start|>(x1,y1)<|box_end|>')
drag(start_box='<|box_start|>(x1,y1)<|box_end|>', end_box='<|box_start|>(x3,y3)<|box_end|>')
hotkey(key='')
type(content='') #If you want to submit your input, use "\\n" at the end of `content`.
scroll(start_box='<|box_start|>(x1,y1)<|box_end|>', direction='down or up or right or left')
wait() #Sleep for 5s and take a screenshot to check for any changes.
finished() # Submit the task regardless of whether it succeeds or fails.

## Note
- Use {language} in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
{instruction}
"""

# Shared state for the running task
_task_state: dict[str, Any] = {"running": False, "log": [], "status": "idle"}
_task_lock = threading.Lock()


def _log(msg: str) -> None:
    with _task_lock:
        _task_state["log"].append(msg)


def set_dpi_awareness() -> None:
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_browser_hwnd() -> int:
    """Find the React UI browser tab by its exact title 'CUA AIPC'."""
    found: list[int] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)

    def enum_cb(hwnd: int, _: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            # Only match the exact React app tab — never touch anything else
            if "CUA AIPC" in title:
                found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
    return found[0] if found else 0


def capture_screen() -> tuple[str, tuple[int, int]]:
    image = ImageGrab.grab().convert("RGB")
    w, h = image.size
    long_side = max(w, h)
    if long_side > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / long_side
        w, h = round(w * scale), round(h * scale)
        image = image.resize((w, h), Image.LANCZOS)
    buf = BytesIO()
    image.save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    return url, (w, h)


def compact_response(raw: str) -> str:
    return f"Action: {parse_action(raw)}"


def build_messages(prompt: str, screenshots: list[str], responses: list[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a helpful assistant."}
    ]
    n = len(screenshots)
    img_start = max(0, n - MAX_IMAGES)
    for i in range(n):
        content: list[dict[str, Any]] = []
        if i == 0:
            content.append({"type": "text", "text": prompt})
        if i >= img_start:
            content.append({"type": "image_url", "image_url": {"url": screenshots[i]}})
        elif i != 0:
            content.append({"type": "text", "text": "[earlier screenshot omitted]"})
        messages.append({"role": "user", "content": content})
        if i < len(responses):
            resp = responses[i] if i >= img_start else compact_response(responses[i])
            messages.append({"role": "assistant", "content": resp})
    return messages


def ask_model(messages: list[dict[str, Any]]) -> str:
    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 512,
        "frequency_penalty": 1,
    }
    response = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def parse_action(raw: str) -> str:
    cleaned = raw.replace("```", "").strip()
    for line in cleaned.splitlines():
        s = line.strip()
        if s.lower().startswith("action:"):
            return s[len("action:"):].strip()
    verb = re.compile(
        r"^(click|left_double|right_single|drag|hotkey|type|scroll|wait|finished|stop|press)\b",
        re.I,
    )
    for line in cleaned.splitlines():
        s = line.strip()
        if verb.match(s):
            return s
    return cleaned


def normalize_key(key: str) -> str:
    key = key.strip().lower()
    mapping = {
        "win": "winleft", "windows": "winleft", "cmd": "winleft",
        "meta": "winleft", "super": "winleft",
        "return": "enter", "esc": "escape", "del": "delete",
        "control": "ctrl",
    }
    return mapping.get(key, key)


def to_screen_xy(x: float, y: float, img_size: tuple[int, int]) -> tuple[int, int]:
    screen_w, screen_h = pyautogui.size()
    if COORD_SPACE == "normalized_1000":
        x = max(0.0, min(1000.0, x))
        y = max(0.0, min(1000.0, y))
        return round(screen_w * x / 1000), round(screen_h * y / 1000)
    img_w, img_h = img_size
    return round(x * screen_w / img_w), round(y * screen_h / img_h)


def coords_in(action: str) -> list[float]:
    inner = action[action.find("(") + 1: action.rfind(")")] if "(" in action else action
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", inner)]


def point_from(action: str, img_size: tuple[int, int], box_key: str = "start_box") -> tuple[int, int]:
    seg = action
    if box_key in action:
        seg = action.split(box_key, 1)[1]
        end = seg.find(")")
        seg = seg[: end + 1] if end != -1 else seg
    nums = coords_in(seg)
    if len(nums) >= 4:
        return to_screen_xy((nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2, img_size)
    if len(nums) >= 2:
        return to_screen_xy(nums[0], nums[1], img_size)
    raise ValueError(f"No coordinates in action: {action}")


def execute_action(action: str, img_size: tuple[int, int]) -> bool:
    lower = action.lower()

    if lower.startswith(("finished", "stop", "call_user")):
        return False

    if lower.startswith("wait"):
        _log("Executing: wait")
        if not DRY_RUN:
            time.sleep(2)
        return True

    if lower.startswith("hotkey"):
        m = re.search(r"keys?\s*=\s*['\"](.+?)['\"]", action, re.I)
        if not m:
            raise ValueError(f"Bad hotkey: {action}")
        keys = [normalize_key(k) for k in re.split(r"[+\s]+", m.group(1).strip()) if k]
        _log(f"Executing hotkey: {keys}")
        if not DRY_RUN:
            pyautogui.press(keys[0]) if len(keys) == 1 else pyautogui.hotkey(*keys)
        return True

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
        text = text.rstrip("\n")
        _log(f"Executing type: {text!r} (submit={submit})")
        if not DRY_RUN:
            time.sleep(0.3)
            pyautogui.write(text, interval=0.03)
            if submit:
                pyautogui.press("enter")
        return True

    if lower.startswith("scroll"):
        m = re.search(r"direction\s*=\s*['\"](\w+)", action, re.I)
        direction = (m.group(1).lower() if m else "down")
        _log(f"Executing scroll: {direction}")
        if not DRY_RUN:
            try:
                x, y = point_from(action, img_size)
                pyautogui.moveTo(x, y)
            except ValueError:
                pass
            amount = 500 if direction in ("up", "left") else -500
            pyautogui.scroll(amount)
        return True

    if lower.startswith("drag"):
        sx, sy = point_from(action, img_size, "start_box")
        ex, ey = point_from(action, img_size, "end_box")
        _log(f"Executing drag: ({sx},{sy}) -> ({ex},{ey})")
        if not DRY_RUN:
            pyautogui.moveTo(sx, sy)
            pyautogui.dragTo(ex, ey, duration=0.4, button="left")
        return True

    if lower.startswith("left_double"):
        x, y = point_from(action, img_size)
        _log(f"Executing double click: ({x}, {y})")
        if not DRY_RUN:
            pyautogui.doubleClick(x, y)
        return True

    if lower.startswith("right_single"):
        x, y = point_from(action, img_size)
        _log(f"Executing right click: ({x}, {y})")
        if not DRY_RUN:
            pyautogui.click(x, y, button="right")
        return True

    if lower.startswith("click"):
        x, y = point_from(action, img_size)
        _log(f"Executing click: ({x}, {y})")
        if not DRY_RUN:
            pyautogui.click(x, y)
        return True

    raise ValueError(f"Unsupported action: {action}")


def run_agent(app_name: str) -> None:
    set_dpi_awareness()
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.2

    with _task_lock:
        _task_state["running"] = True
        _task_state["log"] = []
        _task_state["status"] = "running"

    task = (
        f"Open the {app_name} app on Windows 11.\n"
        f"Step 1: Press the Windows key using hotkey(key='win') to open the Start menu.\n"
        f"Step 2: Type '{app_name}' and press Enter using type(content='{app_name}\\n').\n"
        f"Step 3: When you see the {app_name} window open on screen, call finished()."
    )
    prompt = COMPUTER_USE.format(language=LANGUAGE, instruction=task)

    screenshots: list[str] = []
    img_sizes: list[tuple[int, int]] = []
    responses: list[str] = []

    _log(f"Starting agent for: {app_name}")

    # Find the browser window once — only used to move it out of the way
    # Never touches any other app (VS Code, terminals, etc.)
    browser_hwnd = get_browser_hwnd()
    if browser_hwnd:
        _log(f"Found browser window, moving aside.")
        # Move the browser window to bottom-right so it's out of the screenshot
        # but NOT minimized — avoids touching other apps entirely
        screen_w, screen_h = pyautogui.size()
        ctypes.windll.user32.SetWindowPos(
            browser_hwnd, 0,
            screen_w, screen_h,  # position off-screen to the right/bottom
            800, 600,             # keep a reasonable size
            0x0040                # SWP_SHOWWINDOW
        )
        time.sleep(0.5)
    else:
        _log("[warn] Browser window not found — continuing anyway.")

    success = False
    for step in range(1, MAX_STEPS + 1):
        url, img_size = capture_screen()
        screenshots.append(url)
        img_sizes.append(img_size)
        messages = build_messages(prompt, screenshots, responses)

        try:
            raw = ask_model(messages)
        except Exception as exc:
            _log(f"[error] model request failed: {exc}")
            break

        _log(f"--- Step {step} ---")
        _log(f"Model: {raw}")
        responses.append(raw)

        action = parse_action(raw)
        _log(f"Action: {action}")

        recent = [parse_action(r) for r in responses[-3:]]
        if len(recent) == 3 and len(set(recent)) == 1:
            _log("[stop] Model repeating same action 3x.")
            break

        if action.lower().startswith(("finished", "stop", "call_user")):
            success = True
            break

        try:
            if not execute_action(action, img_size):
                break
        except Exception as exc:
            _log(f"[error] could not execute action: {exc}")
            continue

        time.sleep(1.5)

    # Move browser window back to center
    if browser_hwnd:
        screen_w, screen_h = pyautogui.size()
        win_w, win_h = 1000, 700
        ctypes.windll.user32.SetWindowPos(
            browser_hwnd, 0,
            (screen_w - win_w) // 2, (screen_h - win_h) // 2,
            win_w, win_h,
            0x0040
        )

    if success:
        _log(f"Successfully opened {app_name}!")
        with _task_lock:
            _task_state["status"] = "success"
    else:
        _log("Task finished without confirmation.")
        with _task_lock:
            _task_state["status"] = "done"

    with _task_lock:
        _task_state["running"] = False


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json()
    app_name = (data or {}).get("app", "").strip()
    if not app_name:
        return jsonify({"error": "app name is required"}), 400
    with _task_lock:
        if _task_state["running"]:
            return jsonify({"error": "agent already running"}), 409
    threading.Thread(target=run_agent, args=(app_name,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/status", methods=["GET"])
def status():
    with _task_lock:
        return jsonify({
            "running": _task_state["running"],
            "status": _task_state["status"],
            "log": list(_task_state["log"]),
        })


if __name__ == "__main__":
    app.run(port=5000, debug=False)
