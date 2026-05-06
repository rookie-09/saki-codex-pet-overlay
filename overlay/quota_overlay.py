import ctypes
import json
import queue
import re
import subprocess
import threading
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont


REFRESH_MS = 1_000
INFO_REFRESH_MS = 250
FOLLOW_POLL_MS = 33
ANIMATE_MS = 16
SNAP_DISTANCE = 120
PET_MISS_TOLERANCE = 8
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WIDTH = 220
HEIGHT = 112
COLLAPSED_WIDTH = 44
COLLAPSED_HEIGHT = 36
BUBBLE_WIDTH = 130
BUBBLE_HEIGHT = 74
MASCOT_LEFT = 244
MASCOT_TOP = 191
MASCOT_WIDTH = 112
MASCOT_HEIGHT = 121
TEXT = {
    "zh": {
        "stamina": "\u4f53\u529b\u503c",
        "model": "\u6a21\u578b",
        "today": "\u4eca\u65e5\u5bf9\u8bdd",
        "name": "\u540d\u79f0",
        "fold": "\u6298\u53e0",
        "language": "\u8bed\u8a00",
        "color": "\u989c\u8272\u98ce\u683c",
    },
    "en": {
        "stamina": "Stamina",
        "model": "Model",
        "today": "Chats",
        "name": "Name",
        "fold": "Fold",
        "language": "Lang",
        "color": "Color",
    },
}
THEMES = [
    {"name": "blue",   "accent": "#3a8ee6", "accent2": "#7cb8f0", "heart": "#4fa0ff", "outline": "#cfe5f4", "soft": "#edf6fc", "button": "#ffffff", "track": "#e6eff6", "ink": "#1d2a35", "muted": "#7a8b97"},
    {"name": "pink",   "accent": "#ec6ba0", "accent2": "#f4a3c4", "heart": "#ff5d8f", "outline": "#f3cfdf", "soft": "#fdf1f7", "button": "#ffffff", "track": "#f6e1ec", "ink": "#3a1d2a", "muted": "#9b7589"},
    {"name": "mint",   "accent": "#2bb88a", "accent2": "#74d4b3", "heart": "#3acba1", "outline": "#c2e6d6", "soft": "#edfaf3", "button": "#ffffff", "track": "#dceee5", "ink": "#1a3229", "muted": "#6e8a7f"},
    {"name": "gold",   "accent": "#d99b1c", "accent2": "#ebbe5a", "heart": "#e8a93a", "outline": "#ebd5a4", "soft": "#fdf6e6", "button": "#ffffff", "track": "#f1e7cd", "ink": "#3a2d10", "muted": "#998754"},
    {"name": "violet", "accent": "#7a63ee", "accent2": "#a89bf2", "heart": "#9376ff", "outline": "#d2c9f3", "soft": "#f3effe", "button": "#ffffff", "track": "#e6e0f4", "ink": "#231a3d", "muted": "#7d7499"},
]

CODEX_EXE = (
    Path.home()
    / "AppData"
    / "Local"
    / "Packages"
    / "OpenAI.Codex_2p2nqsd0c76g0"
    / "LocalCache"
    / "Local"
    / "OpenAI"
    / "Codex"
    / "bin"
    / "codex.exe"
)
CONFIG_PATH = Path.home() / ".codex" / "config.toml"
SESSIONS_PATH = Path.home() / ".codex" / "sessions"
PETS_PATH = Path.home() / ".codex" / "pets"
GLOBAL_STATE_PATH = Path.home() / ".codex" / ".codex-global-state.json"
OVERLAY_SETTINGS_PATH = Path.home() / ".codex" / "quota-overlay-settings.json"
WORKSPACE_HINT = str(Path(__file__).resolve().parent.parent).lower()
BUILT_IN_PET_NAMES = {
    "codex": "Codex",
    "dewey": "Dewey",
    "null-signal": "Null Signal",
}


class Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top


def find_pet_window():
    user32 = ctypes.windll.user32
    rows = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_windows(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True

        title_len = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, title, title_len + 1)
        if title.value != "Codex":
            return True

        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        if class_name.value != "Chrome_WidgetWin_1":
            return True

        rect = Rect()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if 180 <= rect.width <= 900 and 160 <= rect.height <= 900:
            rows.append((hwnd, rect))
        return True

    user32.EnumWindows(enum_windows, 0)
    if not rows:
        return None
    return min(rows, key=lambda row: row[1].width * row[1].height)


class AppServerClient:
    def __init__(self):
        self.proc = None
        self.next_id = 1
        self.pending = {}
        self.lock = threading.Lock()

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        exe = str(CODEX_EXE) if CODEX_EXE.exists() else "codex"
        self.proc = subprocess.Popen(
            [exe, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "saki-quota-overlay",
                    "title": "Saki Quota Overlay",
                    "version": "0.2.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "thread/started",
                        "thread/status/changed",
                        "thread/tokenUsage/updated",
                    ],
                },
            },
            timeout=12,
        )

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def request(self, method, params=None, timeout=15):
        self.start()
        with self.lock:
            req_id = self.next_id
            self.next_id += 1
            event = threading.Event()
            self.pending[req_id] = {"event": event, "message": None}
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        if not event.wait(timeout):
            self.pending.pop(req_id, None)
            raise TimeoutError(f"{method} timed out")
        message = self.pending.pop(req_id)["message"]
        if "error" in message:
            raise RuntimeError(message["error"])
        return message.get("result")

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = message.get("id")
            if msg_id in self.pending:
                self.pending[msg_id]["message"] = message
                self.pending[msg_id]["event"].set()
            elif msg_id is not None and message.get("method"):
                self._reply_unsupported(msg_id)

    def _reply_unsupported(self, msg_id):
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": "Unsupported client callback"},
            }
            self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def _drain_stderr(self):
        for _ in self.proc.stderr:
            pass


def remaining(window):
    if not window:
        return None
    used = window.get("usedPercent")
    if used is None:
        return None
    return max(0, min(100, 100 - int(used)))


def used_percent(window):
    if not window:
        return None
    used = window.get("usedPercent")
    if used is None:
        return None
    return max(0, min(100, int(used)))


def duration_label(window):
    mins = (window or {}).get("windowDurationMins")
    if mins == 300:
        return "5h"
    if mins == 10080:
        return "7d"
    if mins:
        hours = int(mins) // 60
        return f"{hours}h" if hours else f"{mins}m"
    return "--"


def reset_label(window):
    ts = (window or {}).get("resetsAt")
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")


def reset_countdown(window):
    ts = (window or {}).get("resetsAt")
    if not ts:
        return "--"
    seconds = max(0, int(ts - datetime.now(timezone.utc).timestamp()))
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def pick_bucket(result):
    buckets = result.get("rateLimitsByLimitId") or {}
    return buckets.get("codex") or result.get("rateLimits") or {}


def read_configured_model():
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'(?m)^\s*model\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1) if match else "unknown"


def read_active_model():
    if not SESSIONS_PATH.exists():
        return read_configured_model()

    recent_files = []
    for path in SESSIONS_PATH.rglob("*.jsonl"):
        try:
            recent_files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not recent_files:
        return read_configured_model()

    recent_files.sort(reverse=True)
    for _mtime, path in recent_files[:40]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("type") != "turn_context":
                continue
            payload = item.get("payload") or {}
            model = (payload.get("model") or "").strip()
            if not model:
                continue
            cwd = str(payload.get("cwd") or "").lower()
            if cwd and (cwd == WORKSPACE_HINT or cwd.startswith(WORKSPACE_HINT)):
                return model
    return read_configured_model()


def display_model_name(model):
    return (model or "unknown").replace("-", "")


def pet_id_to_display_name(pet_id):
    return " ".join(part.capitalize() for part in pet_id.replace("_", "-").split("-") if part)


def selected_pet_parts(selected):
    if not selected:
        return None, None
    pet_id = selected.split(":", 1)[1] if selected.startswith("custom:") else selected
    return selected, pet_id


def read_overlay_settings():
    try:
        payload = json.loads(OVERLAY_SETTINGS_PATH.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_overlay_settings(settings):
    OVERLAY_SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def should_show_initial_settings():
    return not bool(read_overlay_settings().get("has_seen_initial_settings"))


def mark_initial_settings_seen():
    settings = read_overlay_settings()
    settings["has_seen_initial_settings"] = True
    write_overlay_settings(settings)


def read_display_name_override(selected, pet_id):
    names = read_overlay_settings().get("display_names") or {}
    for key in (selected, pet_id):
        value = names.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def write_display_name_override(selected, pet_id, display_name):
    settings = read_overlay_settings()
    names = settings.get("display_names")
    if not isinstance(names, dict):
        names = {}
    names[selected or pet_id or "default"] = display_name.strip()
    settings["display_names"] = names
    write_overlay_settings(settings)


def read_selected_pet_id():
    try:
        payload = json.loads(GLOBAL_STATE_PATH.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    persisted = payload.get("electron-persisted-atom-state") or {}
    selected = persisted.get("selected-avatar-id")
    return selected.strip() if isinstance(selected, str) and selected.strip() else None


def read_pet_manifest_name(pet_id):
    manifest = PETS_PATH / pet_id / "pet.json"
    if not manifest.exists():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        display_name = (payload.get("displayName") or payload.get("id") or "").strip()
        return display_name or None
    except Exception:
        return None


def read_current_pet_name():
    selected = read_selected_pet_id()
    if selected:
        selected_key, pet_id = selected_pet_parts(selected)
        override = read_display_name_override(selected_key, pet_id)
        if override:
            return override
        custom_name = read_pet_manifest_name(pet_id)
        if custom_name:
            return custom_name
        built_in_name = BUILT_IN_PET_NAMES.get(pet_id.lower())
        if built_in_name:
            return built_in_name
        return pet_id_to_display_name(pet_id)

    if not PETS_PATH.exists():
        return "Pet"
    try:
        pet_dirs = [item for item in PETS_PATH.iterdir() if item.is_dir()]
    except OSError:
        return "Pet"
    if not pet_dirs:
        return "Pet"

    def pet_score(path):
        score = 0.0
        try:
            score = max(score, path.stat().st_mtime)
        except OSError:
            pass
        manifest = path / "pet.json"
        try:
            if manifest.exists():
                score = max(score, manifest.stat().st_mtime)
        except OSError:
            pass
        return score

    active = max(pet_dirs, key=pet_score)
    manifest_name = read_pet_manifest_name(active.name)
    if manifest_name:
        return manifest_name
    return active.name


def today_conversation_count():
    today = datetime.now().astimezone().date()
    paths = set()
    for offset in (-1, 0, 1):
        day = today + timedelta(days=offset)
        day_dir = SESSIONS_PATH / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if day_dir.exists():
            paths.update(day_dir.glob("*.jsonl"))
    if SESSIONS_PATH.exists():
        for path in SESSIONS_PATH.rglob("*.jsonl"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone().date()
            except OSError:
                continue
            if modified == today:
                paths.add(path)
    if not paths and SESSIONS_PATH.exists():
        paths.update(SESSIONS_PATH.rglob("*.jsonl"))
    if not paths:
        return None

    count = 0
    for path in sorted(paths):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
                if item.get("type") != "event_msg":
                    continue
                payload = item.get("payload") or {}
                if payload.get("type") != "user_message":
                    continue
                timestamp = item.get("timestamp")
                if not timestamp:
                    continue
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
                if dt.date() == today:
                    count += 1
            except Exception:
                continue
    return count


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def set_round_rect_coords(canvas, item, x1, y1, x2, y2, radius):
    x2 = max(x1 + 1, x2)
    radius = min(radius, max(0.5, (x2 - x1) / 2))
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    canvas.coords(item, *points)


def follow_value(current, target):
    if current is None:
        return target
    delta = target - current
    if abs(delta) >= SNAP_DISTANCE:
        return target
    next_value = current + delta * 0.52
    return target if abs(target - next_value) < 0.5 else next_value


def place_below_pet(widget, pet_hwnd):
    try:
        hwnd = widget.winfo_id()
    except tk.TclError:
        return
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
    ctypes.windll.user32.SetWindowPos(hwnd, pet_hwnd, 0, 0, 0, 0, flags)


class QuotaOverlay:
    def __init__(self):
        self.client = AppServerClient()
        self.updates = queue.Queue()
        self.last_rect = None
        self.pet_hwnd = None
        self.target_x = None
        self.target_y = None
        self.current_x = None
        self.current_y = None
        self.fetching = False
        self.info_fetching = False
        self.collapsed = False
        self.settings_open = should_show_initial_settings()
        self.name_entry = None
        self.name_var = None
        self.language = "zh"
        self.theme_index = 0
        self.view_width = WIDTH
        self.view_height = HEIGHT
        self.last_primary_used = 0
        self.last_secondary_used = 0
        self.bubble_current_x = None
        self.bubble_current_y = None
        self.bubble_target_x = None
        self.bubble_target_y = None
        self.pet_miss_count = 0
        self.mood_index = 0
        self.pet_name = read_current_pet_name()
        self.model_name = read_active_model()
        self.today_count = today_conversation_count()

        self.root = tk.Tk()
        self.root.title(f"{self.pet_name} Quota")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg="#ff00ff")
        try:
            self.root.wm_attributes("-transparentcolor", "#ff00ff")
        except tk.TclError:
            pass
        self.root.geometry(f"{WIDTH}x{HEIGHT}+1040+690")

        self.canvas = tk.Canvas(
            self.root,
            width=self.view_width,
            height=self.view_height,
            bg="#ff00ff",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.draw_static()
        self.create_bubble()

        self.canvas.bind("<Double-Button-1>", lambda _e: self.refresh_now())
        self.canvas.bind("<Button-1>", self.on_click)

        self.root.after(100, self.poll_updates)
        self.root.after(100, self.update_follow_target)
        self.root.after(100, self.animate_position)
        self.refresh_now()
        self.schedule_refresh()
        self.refresh_info_now()
        self.schedule_info_refresh()
        if self.settings_open:
            mark_initial_settings_seen()

    @property
    def colors(self):
        return THEMES[self.theme_index]

    def label(self, key):
        return TEXT.get(self.language, TEXT["zh"]).get(key, key)

    def model_label_x(self):
        return 11 if self.language == "en" else 15

    def layout_model_row(self):
        if not hasattr(self, "bubble_canvas"):
            return
        if not hasattr(self, "mood_text") or not hasattr(self, "model_label_text"):
            return

        label_font = tkfont.Font(family="Microsoft YaHei UI", size=8, weight="bold")
        value_font = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        label_x = self.model_label_x()
        label_text = self.label("model")
        value_text = display_model_name(self.model_name)

        self.bubble_canvas.coords(self.model_label_text, label_x, 36)
        self.bubble_canvas.itemconfigure(self.model_label_text, text=label_text)

        label_width = label_font.measure(label_text)
        area_left = max(43, label_x + label_width + 5)
        area_right = BUBBLE_WIDTH - 14
        available = max(1, area_right - area_left)
        text_width = value_font.measure(value_text)

        if text_width < available:
            value_x = area_left + (available - text_width) / 2
            value_width = text_width + 2
        else:
            value_x = area_left
            value_width = available

        self.bubble_canvas.coords(self.mood_text, value_x, 36)
        self.bubble_canvas.itemconfigure(self.mood_text, width=value_width)

    def create_bubble(self):
        self.bubble = tk.Toplevel(self.root)
        self.bubble.title(f"{self.pet_name} Mood")
        self.bubble.overrideredirect(True)
        self.bubble.attributes("-topmost", True)
        self.bubble.attributes("-alpha", 0.94)
        self.bubble.configure(bg="#ff00ff")
        try:
            self.bubble.wm_attributes("-transparentcolor", "#ff00ff")
        except tk.TclError:
            pass
        self.bubble.geometry(f"{BUBBLE_WIDTH}x{BUBBLE_HEIGHT}+900+500")
        self.bubble_canvas = tk.Canvas(
            self.bubble,
            width=BUBBLE_WIDTH,
            height=BUBBLE_HEIGHT,
            bg="#ff00ff",
            highlightthickness=0,
            bd=0,
        )
        self.bubble_canvas.pack(fill="both", expand=True)
        self.draw_bubble()
        self.bubble_canvas.bind("<Button-1>", self.on_bubble_click)

    def draw_bubble(self):
        self.bubble_canvas.delete("all")
        colors = self.colors
        rounded_rect(self.bubble_canvas, 3, 5, BUBBLE_WIDTH - 8, BUBBLE_HEIGHT - 6, 14, fill="#f8feff", outline=colors["outline"], width=1)
        self.bubble_canvas.create_polygon(
            BUBBLE_WIDTH - 10,
            34,
            BUBBLE_WIDTH - 2,
            39,
            BUBBLE_WIDTH - 10,
            44,
            fill="#f8feff",
            outline=colors["outline"],
        )
        rounded_rect(
            self.bubble_canvas,
            BUBBLE_WIDTH - 31,
            8,
            BUBBLE_WIDTH - 12,
            27,
            9,
            fill="#f8feff",
            outline=colors["outline"],
            tags=("close",),
        )
        self.bubble_canvas.create_text(
            BUBBLE_WIDTH - 21,
            17,
            text="\u00d7",
            fill=colors["accent"],
            font=("Segoe UI", 11, "bold"),
            tags=("close",),
        )
        self.pet_name_text = self.bubble_canvas.create_text(
            15,
            17,
            anchor="w",
            text=f"\u266a {self.pet_name}",
            fill=colors["accent"],
            font=("Segoe UI", 9, "bold"),
        )
        self.model_label_text = self.bubble_canvas.create_text(
            self.model_label_x(),
            36,
            anchor="w",
            text=self.label("model"),
            fill=colors["muted"],
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.mood_text = self.bubble_canvas.create_text(
            50,
            36,
            anchor="w",
            width=BUBBLE_WIDTH - 60,
            text=display_model_name(self.model_name),
            fill="#6d818b",
            font=("Segoe UI", 8, "bold"),
        )
        self.layout_model_row()
        self.bubble_canvas.create_text(
            15,
            55,
            anchor="w",
            text=self.label("today"),
            fill=colors["muted"],
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.today_text = self.bubble_canvas.create_text(
            78,
            55,
            anchor="w",
            text=self.today_count_label(),
            fill="#6d818b",
            font=("Segoe UI", 8, "bold"),
        )

    def today_count_label(self):
        return "--" if self.today_count is None else str(self.today_count)

    def refresh_info_label(self):
        self.pet_name = read_current_pet_name()
        self.model_name = read_active_model()
        self.today_count = today_conversation_count()
        self.apply_info_update(self.pet_name, self.model_name, self.today_count)

    def apply_info_update(self, pet_name, model_name, today_count):
        self.pet_name = pet_name
        self.model_name = model_name
        self.today_count = today_count
        self.root.title(f"{self.pet_name} Quota")
        self.bubble.title(f"{self.pet_name} Mood")
        self.bubble_canvas.itemconfigure(self.pet_name_text, text=f"\u266a {self.pet_name}")
        self.bubble_canvas.itemconfigure(self.mood_text, text=display_model_name(self.model_name))
        self.layout_model_row()
        self.bubble_canvas.itemconfigure(self.today_text, text=self.today_count_label())

    def on_bubble_click(self, event):
        hit = self.bubble_canvas.find_withtag("current")
        if hit and "close" in self.bubble_canvas.gettags(hit[0]):
            self.close()
            return
        self.refresh_info_now()

    def draw_static(self):
        self.destroy_name_entry()
        self.canvas.delete("all")
        colors = self.colors
        if self.collapsed:
            rounded_rect(self.canvas, 2, 3, COLLAPSED_WIDTH - 2, COLLAPSED_HEIGHT - 3, 14, fill=colors["soft"], outline=colors["outline"], width=1)
            rounded_rect(self.canvas, 7, 8, COLLAPSED_WIDTH - 7, COLLAPSED_HEIGHT - 8, 9, fill="#f8feff", outline="")
            self.canvas.create_oval(12, 8, 34, 30, outline=colors["track"], width=4, tags=("toggle",))
            self.collapsed_arc = self.canvas.create_arc(
                12,
                8,
                34,
                30,
                start=90,
                extent=0,
                style=tk.ARC,
                outline=colors["accent"],
                width=4,
                tags=("toggle",),
            )
            self.canvas.create_oval(20, 16, 26, 22, fill="#f8feff", outline="", tags=("toggle",))
            self.main_text = None
            self.sub_text = None
            self.primary_meta = None
            self.secondary_meta = None
            self.primary_bar_fg = None
            self.secondary_bar_fg = None
            self.primary_bar_bg = None
            self.secondary_bar_bg = None
            self.reset_text = None
            self.update_collapsed_pie()
            return

        rounded_rect(self.canvas, 3, 4, WIDTH - 3, HEIGHT - 5, 18, fill=colors["soft"], outline=colors["outline"], width=1)
        rounded_rect(self.canvas, 9, 10, WIDTH - 9, HEIGHT - 11, 14, fill="#f8feff", outline="")
        self.canvas.create_text(24, 18, anchor="center", text="\u2665", fill=colors["heart"], font=("Segoe UI Symbol", 12, "bold"))
        self.canvas.create_text(39, 18, anchor="w", text=self.label("stamina"), fill=colors["accent"], font=("Segoe UI", 9, "bold"))
        rounded_rect(self.canvas, WIDTH - 34, 10, WIDTH - 14, 30, 10, fill=colors["soft"], outline=colors["outline"], tags=("settings",))
        self.canvas.create_text(WIDTH - 24, 19, text="\u2699", fill=colors["accent"], font=("Segoe UI Symbol", 12, "bold"), tags=("settings",))

        self.main_text = self.canvas.create_text(18, 38, anchor="w", text="5h", fill=colors["muted"], font=("Segoe UI", 8, "bold"))
        self.sub_text = self.canvas.create_text(
            18, 72, anchor="w", text="7d", fill=colors["muted"], font=("Segoe UI", 8, "bold")
        )
        self.primary_meta = self.canvas.create_text(
            WIDTH - 18, 38, anchor="e", text="--% | --", fill=colors["muted"], font=("Segoe UI", 8, "bold")
        )
        self.secondary_meta = self.canvas.create_text(
            WIDTH - 18, 72, anchor="e", text="--% | --", fill=colors["muted"], font=("Segoe UI", 8, "bold")
        )
        self.primary_bar_bg = rounded_rect(self.canvas, 18, 49, WIDTH - 18, 55, 3, fill="#ffffff", outline="#e7f1f5")
        self.primary_bar_fg = rounded_rect(self.canvas, 18, 49, 18, 55, 3, fill=colors["accent"], outline="")
        self.secondary_bar_bg = rounded_rect(self.canvas, 18, 83, WIDTH - 18, 89, 3, fill="#ffffff", outline="#e7f1f5")
        self.secondary_bar_fg = rounded_rect(self.canvas, 18, 83, 18, 89, 3, fill=colors["accent"], outline="")
        self.reset_text = None
        self.collapsed_arc = None
        self.update_usage_bars()
        if self.settings_open:
            self.draw_settings_panel()

    def draw_settings_panel(self):
        colors = self.colors
        x1, y1, x2, y2 = 76, 31, WIDTH - 10, HEIGHT - 9
        rounded_rect(self.canvas, x1, y1, x2, y2, 12, fill="#f8feff", outline=colors["outline"], width=1, tags=("settings_panel",))

        self.canvas.create_text(88, 40, anchor="w", text=self.label("fold"), fill=colors["muted"], font=("Segoe UI", 8, "bold"), tags=("settings_panel",))
        rounded_rect(self.canvas, 176, 31, 201, 49, 6, fill=colors["button"], outline=colors["outline"], width=1, tags=("fold",))
        self.canvas.create_line(182, 40, 195, 40, fill=colors["accent"], width=3, capstyle=tk.ROUND, tags=("fold",))

        self.canvas.create_text(88, 58, anchor="w", text=self.label("name"), fill=colors["muted"], font=("Segoe UI", 8, "bold"), tags=("settings_panel",))
        self.create_name_entry()

        self.canvas.create_text(88, 76, anchor="w", text=self.label("language"), fill=colors["muted"], font=("Segoe UI", 8, "bold"), tags=("settings_panel",))
        lang_items = [("en", "ENG", 143, 155), ("zh", "\u4e2d", 169, 181)]
        for lang, text, lx, tx in lang_items:
            selected = self.language == lang
            rounded_rect(
                self.canvas,
                lx,
                68,
                lx + 25,
                85,
                8,
                fill=colors["accent"] if selected else colors["button"],
                outline=colors["accent"] if selected else colors["outline"],
                tags=(f"lang_{lang}",),
            )
            self.canvas.create_text(
                tx,
                76,
                text=text,
                fill="#ffffff" if selected else colors["muted"],
                font=("Segoe UI", 7, "bold"),
                tags=(f"lang_{lang}",),
            )

        self.canvas.create_text(88, 98, anchor="w", text=self.label("color"), fill=colors["muted"], font=("Segoe UI", 8, "bold"), tags=("settings_panel",))
        for index, theme in enumerate(THEMES):
            sx = 135 + index * 14
            tag = f"theme_{index}"
            if index == self.theme_index:
                self.canvas.create_oval(sx - 2, 90, sx + 12, 104, fill=theme["soft"], outline=theme["accent"], width=1, tags=(tag,))
            self.canvas.create_oval(sx, 92, sx + 10, 102, fill=theme["accent"], outline="#ffffff", width=1, tags=(tag,))

    def update_usage_bars(self):
        if self.primary_bar_fg is not None:
            primary_right = 18 + int((WIDTH - 36) * self.last_primary_used / 100)
            set_round_rect_coords(self.canvas, self.primary_bar_fg, 18, 49, max(18, primary_right), 55, 3)
        if self.secondary_bar_fg is not None:
            secondary_right = 18 + int((WIDTH - 36) * self.last_secondary_used / 100)
            set_round_rect_coords(self.canvas, self.secondary_bar_fg, 18, 83, max(18, secondary_right), 89, 3)

    def set_collapsed(self, collapsed):
        if self.collapsed == collapsed:
            return
        self.settings_open = False
        old_width = self.view_width
        old_height = self.view_height
        if self.current_x is not None and self.current_y is not None:
            if collapsed:
                anchor_x = self.current_x + WIDTH - 24
                anchor_y = self.current_y + 19
                self.current_x = anchor_x - COLLAPSED_WIDTH / 2
                self.current_y = anchor_y - COLLAPSED_HEIGHT / 2
            else:
                anchor_x = self.current_x + old_width / 2
                anchor_y = self.current_y + old_height / 2
                self.current_x = anchor_x - (WIDTH - 24)
                self.current_y = anchor_y - 19
        self.collapsed = collapsed
        self.view_width = COLLAPSED_WIDTH if collapsed else WIDTH
        self.view_height = COLLAPSED_HEIGHT if collapsed else HEIGHT
        self.canvas.configure(width=self.view_width, height=self.view_height)
        self.root.geometry(f"{self.view_width}x{self.view_height}+{int(self.current_x or 0)}+{int(self.current_y or 0)}")
        self.draw_static()
        if collapsed:
            self.bubble.withdraw()
        else:
            self.bubble.deiconify()
        self.update_follow_target()

    def update_collapsed_pie(self):
        if getattr(self, "collapsed_arc", None) is not None:
            self.canvas.itemconfigure(self.collapsed_arc, extent=-3.6 * self.last_primary_used)

    def destroy_name_entry(self):
        if self.name_entry is not None:
            try:
                self.name_entry.destroy()
            except tk.TclError:
                pass
        self.name_entry = None
        self.name_var = None

    def create_name_entry(self):
        self.name_var = tk.StringVar(value=self.pet_name)
        self.name_entry = tk.Entry(
            self.root,
            textvariable=self.name_var,
            font=("Microsoft YaHei UI", 8, "bold"),
            relief="solid",
            bd=1,
            bg="#ffffff",
            fg="#121212",
            insertbackground="#121212",
            highlightthickness=1,
            highlightbackground=self.colors["outline"],
            highlightcolor=self.colors["accent"],
        )
        self.name_entry.bind("<Return>", self.save_name_entry)
        self.name_entry.bind("<FocusOut>", self.save_name_entry)
        self.canvas.create_window(134, 58, anchor="w", width=68, height=18, window=self.name_entry, tags=("settings_panel",))

    def save_name_entry(self, _event=None):
        if self.name_var is None:
            return
        selected = read_selected_pet_id()
        selected_key, pet_id = selected_pet_parts(selected)
        if not pet_id:
            pet_id = "default"
        display_name = self.name_var.get().strip()
        if not display_name or display_name == self.pet_name:
            return
        write_display_name_override(selected_key, pet_id, display_name)
        self.refresh_info_label()
        if self.settings_open:
            self.draw_static()

    def on_click(self, event):
        if self.collapsed:
            self.set_collapsed(False)
            return
        hit = self.canvas.find_withtag("current")
        tags = self.canvas.gettags(hit[0]) if hit else ()
        if "settings" in tags:
            self.settings_open = not self.settings_open
            self.draw_static()
            return
        if "fold" in tags:
            self.set_collapsed(True)
            return
        if "lang_en" in tags:
            self.language = "en"
            self.draw_static()
            self.draw_bubble()
            return
        if "lang_zh" in tags:
            self.language = "zh"
            self.draw_static()
            self.draw_bubble()
            return
        for tag in tags:
            if tag.startswith("theme_"):
                try:
                    self.theme_index = int(tag.split("_", 1)[1])
                except (IndexError, ValueError):
                    return
                self.draw_static()
                self.draw_bubble()
                return

    def refresh_now(self):
        if self.fetching:
            return
        self.fetching = True
        threading.Thread(target=self.fetch_quota, daemon=True).start()

    def schedule_refresh(self):
        self.root.after(REFRESH_MS, self._scheduled_refresh)

    def _scheduled_refresh(self):
        self.refresh_now()
        self.schedule_refresh()

    def refresh_info_now(self):
        if self.info_fetching:
            return
        self.info_fetching = True
        threading.Thread(target=self.fetch_info, daemon=True).start()

    def fetch_info(self):
        try:
            self.updates.put(
                (
                    "info",
                    {
                        "pet_name": read_current_pet_name(),
                        "model_name": read_active_model(),
                        "today_count": today_conversation_count(),
                    },
                )
            )
        finally:
            self.info_fetching = False

    def schedule_info_refresh(self):
        self.root.after(INFO_REFRESH_MS, self._scheduled_info_refresh)

    def _scheduled_info_refresh(self):
        self.refresh_info_now()
        self.schedule_info_refresh()

    def update_follow_target(self):
        pet = find_pet_window()
        if pet is not None:
            pet_hwnd, rect = pet
            self.pet_miss_count = 0
            self.last_rect = rect
            self.pet_hwnd = pet_hwnd
            screen_w = ctypes.windll.user32.GetSystemMetrics(0)
            screen_h = ctypes.windll.user32.GetSystemMetrics(1)
            expanded_x = rect.right - WIDTH - 18
            expanded_y = rect.bottom - 3
            bubble_x = expanded_x
            bubble_y = expanded_y - BUBBLE_HEIGHT - 4
            if self.collapsed:
                button_center_x = expanded_x + WIDTH - 24
                button_center_y = expanded_y + 19
                x = button_center_x - self.view_width / 2
                y = button_center_y - self.view_height / 2
            else:
                x = expanded_x
                y = expanded_y
            self.target_x = max(0, min(screen_w - self.view_width, x))
            self.target_y = max(0, min(screen_h - self.view_height, y))
            self.bubble_target_x = max(0, min(screen_w - BUBBLE_WIDTH, bubble_x))
            self.bubble_target_y = max(0, min(screen_h - BUBBLE_HEIGHT, bubble_y))
            if self.current_x is None or self.current_y is None:
                self.current_x = self.target_x
                self.current_y = self.target_y
                self.root.geometry(f"{self.view_width}x{self.view_height}+{self.current_x}+{self.current_y}")
            if self.bubble_current_x is None or self.bubble_current_y is None:
                self.bubble_current_x = self.bubble_target_x
                self.bubble_current_y = self.bubble_target_y
                self.bubble.geometry(f"{BUBBLE_WIDTH}x{BUBBLE_HEIGHT}+{self.bubble_current_x}+{self.bubble_current_y}")
            self.root.deiconify()
            place_below_pet(self.root, pet_hwnd)
            if self.collapsed:
                self.bubble.withdraw()
            else:
                self.bubble.deiconify()
                place_below_pet(self.bubble, pet_hwnd)
        else:
            self.pet_miss_count += 1
            if self.pet_miss_count >= PET_MISS_TOLERANCE:
                self.pet_hwnd = None
                self.target_x = None
                self.target_y = None
                self.bubble_target_x = None
                self.bubble_target_y = None
                self.root.withdraw()
                self.bubble.withdraw()
        self.root.after(FOLLOW_POLL_MS, self.update_follow_target)

    def animate_position(self):
        if self.target_x is not None and self.target_y is not None:
            self.current_x = follow_value(self.current_x, self.target_x)
            self.current_y = follow_value(self.current_y, self.target_y)
            self.root.geometry(
                f"{self.view_width}x{self.view_height}+{int(self.current_x)}+{int(self.current_y)}"
            )
            if self.pet_hwnd is not None:
                place_below_pet(self.root, self.pet_hwnd)
        if not self.collapsed and self.bubble_target_x is not None and self.bubble_target_y is not None:
            self.bubble_current_x = follow_value(self.bubble_current_x, self.bubble_target_x)
            self.bubble_current_y = follow_value(self.bubble_current_y, self.bubble_target_y)
            self.bubble.geometry(
                f"{BUBBLE_WIDTH}x{BUBBLE_HEIGHT}+{int(self.bubble_current_x)}+{int(self.bubble_current_y)}"
            )
            if self.pet_hwnd is not None:
                place_below_pet(self.bubble, self.pet_hwnd)
        self.root.after(ANIMATE_MS, self.animate_position)

    def fetch_quota(self):
        try:
            result = self.client.request("account/rateLimits/read")
            bucket = pick_bucket(result)
            primary = bucket.get("primary")
            secondary = bucket.get("secondary")
            self.updates.put(
                (
                    "ok",
                    {
                        "primary_left": remaining(primary),
                        "primary_used": used_percent(primary),
                        "primary_label": duration_label(primary),
                        "primary_reset": reset_label(primary),
                        "primary_countdown": reset_countdown(primary),
                        "secondary_left": remaining(secondary),
                        "secondary_used": used_percent(secondary),
                        "secondary_label": duration_label(secondary),
                        "secondary_reset": reset_label(secondary),
                        "secondary_countdown": reset_countdown(secondary),
                        "plan": bucket.get("planType") or "",
                    },
                )
            )
        except Exception as exc:
            self.updates.put(("error", str(exc)))
        finally:
            self.fetching = False

    def poll_updates(self):
        while True:
            try:
                kind, payload = self.updates.get_nowait()
            except queue.Empty:
                break
            if kind == "info":
                self.apply_info_update(
                    payload["pet_name"], payload["model_name"], payload["today_count"]
                )
            elif kind == "ok":
                p = payload["primary_left"]
                s = payload["secondary_left"]
                primary_used = payload["primary_used"]
                secondary_used = payload["secondary_used"]
                self.last_primary_used = primary_used or 0
                self.last_secondary_used = secondary_used or 0
                primary_meta = "--% | --" if p is None else f'{p}% | {payload["primary_countdown"]}'
                secondary_meta = "--% | --" if s is None else f'{s}% | {payload["secondary_countdown"]}'
                if self.main_text is not None:
                    self.canvas.itemconfigure(self.main_text, text="5h")
                if self.sub_text is not None:
                    self.canvas.itemconfigure(self.sub_text, text="7d")
                if self.primary_meta is not None:
                    self.canvas.itemconfigure(self.primary_meta, text=primary_meta)
                if self.secondary_meta is not None:
                    self.canvas.itemconfigure(self.secondary_meta, text=secondary_meta)
                if self.primary_bar_fg is not None:
                    self.canvas.itemconfigure(self.primary_bar_fg, fill=self.colors["accent"])
                if self.secondary_bar_fg is not None:
                    self.canvas.itemconfigure(self.secondary_bar_fg, fill=self.colors["accent"])
                self.update_usage_bars()
                self.update_collapsed_pie()
                self.root.title(
                    f'5h reset {payload["primary_reset"]} | 7d reset {payload["secondary_reset"]}'
                )
            else:
                if self.main_text is not None:
                    self.canvas.itemconfigure(self.main_text, text="offline")
                if self.sub_text is not None:
                    self.canvas.itemconfigure(self.sub_text, text="retrying")
                if self.primary_meta is not None:
                    self.canvas.itemconfigure(self.primary_meta, text="--%")
                if self.secondary_meta is not None:
                    self.canvas.itemconfigure(self.secondary_meta, text="--%")
        self.root.after(100, self.poll_updates)

    def close(self):
        self.client.stop()
        self.bubble.destroy()
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.mainloop()


if __name__ == "__main__":
    QuotaOverlay().run()
