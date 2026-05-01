#!/usr/bin/env python3
import os
import sys
import traceback

# PyInstaller's Windows no-console/windowed mode sets stdin, stdout, and
# stderr to None. Several GUI/runtime libraries still expect file-like stream
# objects during startup, so install harmless devnull handles before imports.
_STDIO_DEVNULLS = []


def _ensure_standard_streams():
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name, None) is None:
            stream = open(os.devnull, mode, encoding="utf-8", buffering=1)
            _STDIO_DEVNULLS.append(stream)
            setattr(sys, name, stream)
        original_name = f"__{name}__"
        if getattr(sys, original_name, None) is None:
            setattr(sys, original_name, getattr(sys, name))


_ensure_standard_streams()


def _get_config_dir():
    if os.name == "nt":
        return os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "fleshfetch"
        )
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "fleshfetch")


CONFIG_DIR = _get_config_dir()
STARTUP_LOG_FILE = os.path.join(CONFIG_DIR, "startup.log")
_STARTUP_ERROR_SHOWN = False


def _write_startup_log(text):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(STARTUP_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


def _show_startup_error():
    global _STARTUP_ERROR_SHOWN
    if _STARTUP_ERROR_SHOWN:
        return
    _STARTUP_ERROR_SHOWN = True
    if sys.platform != "win32":
        return
    try:
        import ctypes
        message = (
            "Fleshfetch crashed during startup.\n\n"
            "A traceback was written to:\n"
            f"{STARTUP_LOG_FILE}"
        )
        ctypes.windll.user32.MessageBoxW(None, message, "Fleshfetch crashed", 0x10)
    except Exception:
        pass


def _handle_uncaught_exception(exc_type, exc, tb):
    _write_startup_log("\n=== Unhandled exception ===\n")
    _write_startup_log("".join(traceback.format_exception(exc_type, exc, tb)))
    _show_startup_error()
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass


sys.excepthook = _handle_uncaught_exception

os.environ["GDK_SCALE"] = "1"
os.environ["GDK_DPI_SCALE"] = "1"
os.environ["GTK_THEME"] = "Adwaita"
import json
import time
import random
import importlib.machinery
import importlib.util

import requests
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, Gdk, GLib

# ---------- CONSOLE CAPTURE ----------
# Intercept stdout and stderr so we can show them in the in-game Console tab.
# The original streams are preserved so output still goes to the terminal too.

class _TeeStream:
    """Writes to both the original stream and a shared log buffer."""
    def __init__(self, original):
        self._original = original
        self._history = []
        self._callbacks = []  # list of callables notified on each write

    def write(self, text):
        self._history.append(text)
        if self._original is not None:
            try:
                self._original.write(text)
            except Exception:
                pass
        for cb in self._callbacks:
            try:
                cb(text)
            except Exception:
                pass

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def fileno(self):
        try:
            return self._original.fileno()
        except Exception:
            return -1

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._original, "errors", "replace")

_stdout_tee = _TeeStream(sys.stdout)
_stderr_tee = _TeeStream(sys.stderr)
sys.stdout = _stdout_tee
sys.stderr = _stderr_tee

# ---------- OPTIONAL DISCORD RPC ----------
try:
    from pypresence import Presence
    RPC_AVAILABLE = True
except Exception:
    Presence = None
    RPC_AVAILABLE = False

APP_ID = "dev.xtexecute.fleshfetch"
RPC_CLIENT_ID = "1499450242091716659"

# ---------- PATHS ----------
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
    # When frozen, assets the user places (like click.wav) live next to the exe
    EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    EXE_DIR  = BASE_DIR
INTERNAL_DIR = os.path.join(EXE_DIR, "_internal")


def find_app_asset(filename: str) -> str:
    """Find user-replaceable assets next to the app, then bundled assets."""
    candidates = [
        os.path.join(EXE_DIR, filename),
        os.path.join(INTERNAL_DIR, filename),
        os.path.join(BASE_DIR, filename),
    ]
    seen = set()
    for path in candidates:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.exists(path):
            return path
    return candidates[0]

os.makedirs(CONFIG_DIR, exist_ok=True)

STATE_FILE        = os.path.join(CONFIG_DIR, "state.json")
SETTINGS_FILE     = os.path.join(CONFIG_DIR, "settings.json")
ACHIEVEMENTS_FILE = os.path.join(CONFIG_DIR, "achievements.json")
COUNTER_FILE      = os.path.join(CONFIG_DIR, "flesh_counter.txt")

LEGACY_STATE_FILE        = os.path.join(BASE_DIR, "state.json")
LEGACY_SETTINGS_FILE     = os.path.join(BASE_DIR, "settings.json")
LEGACY_ACHIEVEMENTS_FILE = os.path.join(BASE_DIR, "achievements.json")
LEGACY_COUNTER_FILE      = os.path.join(BASE_DIR, "flesh_counter.txt")

SYSTEM_MODS_DIR = os.path.join(BASE_DIR, "mods")
USER_MODS_DIR   = os.path.join(CONFIG_DIR, "mods")

os.makedirs(USER_MODS_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "enable_rpc": False,
    "squish_ms": 100,
    "play_click_sound": False,
    "click_sound_volume": 15,
}

DEFAULT_STATE = {
    "currencies": {"flesh": 0.0},
    "flesh_per_click": 1.0,
    "upgrades_owned": {},
    "total_clicks": 0,
}

DEFAULT_ACHIEVEMENTS = {
    "first_click":    {"name": "First Click",      "desc": "Click the flesh at least once.",  "unlocked": False},
    "ten_clicks":     {"name": "Ten Clicks",        "desc": "Click the flesh 10 times.",       "unlocked": False},
    "hundred_clicks": {"name": "Hundred Clicks",    "desc": "Click the flesh 100 times.",      "unlocked": False},
    "first_upgrade":  {"name": "First Upgrade",     "desc": "Buy your first upgrade.",         "unlocked": False},
    "five_upgrades":  {"name": "Upgrade Collector", "desc": "Own at least 5 upgrades total.",  "unlocked": False},
    "hundred_flesh":  {"name": "Flesh Pile",        "desc": "Reach 100 flesh.",                "unlocked": False},
    "thousand_flesh": {"name": "Flesh Mountain",    "desc": "Reach 1000 flesh.",               "unlocked": False},
}

# ---------- CURRENCY REGISTRY ----------
# Built-in currencies. Mods add more via game.register_currency().
BASE_CURRENCIES = {
    "flesh": {"display_name": "Flesh"},
}

# ---------- UPGRADE SCHEMA ----------
# Each upgrade defines:
#   base_cost, cost_mult       — scaling cost
#   cost_currency              — which currency is spent (default: "flesh")
#   currency_effects: list of:
#       currency   — registry name of the currency affected
#       cpc        — added to click gain per upgrade owned
#       cps        — added to per-second gain per upgrade owned
#       on_buy     — granted once when the upgrade is purchased
#
# Legacy fps/fpc keys are still accepted transparently.

BASE_UPGRADES = {
    "bigger_clicks": {
        "name": "Bigger Clicks",
        "desc": "+1 flesh per click.",
        "category": "click",
        "base_cost": 10, "cost_mult": 1.15,
        "cost_currency": "flesh",
        "currency_effects": [
            {"currency": "flesh", "cpc": 1.0, "cps": 0.0, "on_buy": 0.0},
        ],
    },
    "auto_clicker_1": {
        "name": "Autoclicker Mk.I",
        "desc": "+1 flesh/sec per unit.",
        "category": "auto",
        "base_cost": 25, "cost_mult": 1.15,
        "cost_currency": "flesh",
        "currency_effects": [
            {"currency": "flesh", "cpc": 0.0, "cps": 1.0, "on_buy": 0.0},
        ],
    },
    "auto_clicker_2": {
        "name": "Autoclicker Mk.II",
        "desc": "+2 flesh/sec per unit.",
        "category": "auto",
        "base_cost": 100, "cost_mult": 1.18,
        "cost_currency": "flesh",
        "currency_effects": [
            {"currency": "flesh", "cpc": 0.0, "cps": 2.0, "on_buy": 0.0},
        ],
    },
    "crit_click": {
        "name": "Critical Clicks",
        "desc": "Chance for double flesh per click.",
        "category": "click",
        "base_cost": 200, "cost_mult": 1.2,
        "cost_currency": "flesh",
        "currency_effects": [],  # handled specially in on_click crit logic
    },
}


# ---------- JSON HELPERS ----------

def load_json(path, default, legacy_path=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default.copy()
    if legacy_path and os.path.exists(legacy_path):
        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            try:
                save_json(path, data)
            except Exception:
                pass
            return data
        except Exception:
            return default.copy()
    return default.copy()


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def load_legacy_counter():
    for p in (COUNTER_FILE, LEGACY_COUNTER_FILE):
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    return int(f.read().strip())
            except Exception:
                continue
    return 0


def save_legacy_counter(value):
    try:
        os.makedirs(os.path.dirname(COUNTER_FILE), exist_ok=True)
        with open(COUNTER_FILE, "w") as f:
            f.write(str(int(value)))
    except Exception:
        pass


# ---------- LEADERBOARD / SUPABASE HELPERS ----------

SUPABASE_URL               = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY               = os.environ.get("SUPABASE_KEY", "")
SUPABASE_LEADERBOARD_TABLE = os.environ.get("SUPABASE_LEADERBOARD_TABLE", "leaderboard")


def get_default_username() -> str:
    try:
        return os.getlogin()
    except Exception:
        pass
    if os.name == "nt":
        name = os.environ.get("USERNAME", "")
        if name:
            return name
    home = os.path.expanduser("~")
    return os.path.basename(home.rstrip(os.sep)) or "unknown"


def _leaderboard_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def submit_leaderboard_entry(username: str, flesh_amount: int):
    if not _leaderboard_configured():
        return False, "SUPABASE_URL / SUPABASE_KEY not configured in environment"
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SUPABASE_LEADERBOARD_TABLE}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    payload = {"username": username, "flesh_amount": int(flesh_amount)}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        return False, f"Request error: {e}"
    if resp.status_code not in (200, 201):
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        data = resp.json()
    except Exception:
        data = {}
    return True, data


def fetch_leaderboard_entries():
    if not _leaderboard_configured():
        return False, "SUPABASE_URL / SUPABASE_KEY not configured in environment", []
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SUPABASE_LEADERBOARD_TABLE}?select=*&order=flesh_amount.desc"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        return False, f"Request error: {e}", []
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}", []
    try:
        rows = resp.json()
    except Exception as e:
        return False, f"JSON decode error: {e}", []
    if not isinstance(rows, list):
        return False, "Unexpected response format", []
    return True, "", rows


# ---------- MAIN WINDOW ----------

class FleshClicker(Gtk.Window):
    def __init__(self, app: Gtk.Application):
        super().__init__(title="Flesh Clicker")
        self.set_default_size(900, 600)
        self.set_application(app)
        self.app = app

        # ---------- load saved data ----------
        state_file_existed = os.path.exists(STATE_FILE)
        self.settings     = load_json(SETTINGS_FILE,     DEFAULT_SETTINGS,     legacy_path=LEGACY_SETTINGS_FILE)
        self.state        = load_json(STATE_FILE,        DEFAULT_STATE,        legacy_path=LEGACY_STATE_FILE)
        self.achievements = load_json(ACHIEVEMENTS_FILE, DEFAULT_ACHIEVEMENTS, legacy_path=LEGACY_ACHIEVEMENTS_FILE)
        self.achievements = {
            k: dict(v) if isinstance(v, dict) else v
            for k, v in self.achievements.items()
        }
        had_currencies_dict = state_file_existed and isinstance(self.state.get("currencies"), dict)

        for k, v in DEFAULT_SETTINGS.items():
            self.settings.setdefault(k, v)
        for k, v in DEFAULT_STATE.items():
            if k not in self.state:
                self.state[k] = v
        if "upgrades_owned" not in self.state:
            self.state["upgrades_owned"] = {}

        # migrate legacy flat "flesh" float -> currencies dict
        if "flesh" in self.state and not isinstance(self.state.get("currencies"), dict):
            self.state["currencies"] = {"flesh": float(self.state.pop("flesh", 0.0))}
        if "currencies" not in self.state:
            self.state["currencies"] = {"flesh": 0.0}
        self.state["currencies"].setdefault("flesh", 0.0)

        for k, v in DEFAULT_ACHIEVEMENTS.items():
            if k not in self.achievements:
                self.achievements[k] = dict(v)
        self._normalize_achievement_sources()

        # sync flesh with the old counter file only for pre-currency saves
        if not had_currencies_dict and self.state["currencies"].get("flesh", 0.0) == 0:
            legacy = load_legacy_counter()
            if legacy > 0:
                self.state["currencies"]["flesh"] = float(legacy)

        # ---------- registries ----------
        self.currencies = dict(BASE_CURRENCIES)
        self.upgrades   = dict(BASE_UPGRADES)

        # primary currency: used for base per-click gain when mods replace vanilla flesh
        self.primary_currency = "flesh"

        # mods can override this to change the clickable image
        self.flesh_image_path = os.path.join(BASE_DIR, "flesh.png")

        # mod tab/button queues — populated during load_mods(), consumed in build_ui()
        # _pending_tabs: list of (tab_id, label, box) tuples
        # _pending_buttons: dict of tab_id -> list of (label, callback) tuples
        self._pending_tabs    = []
        self._pending_buttons = {}
        # map of tab_id -> Gtk.Box (the page widget), filled after build_ui
        self._tab_pages = {}
        self.loaded_mod_ids = set()
        self.installed_mods = []
        self._current_mod_info = None

        # mods can override this to change the click sound
        # default lookup lets users drop in their own wav, then falls back to bundled assets
        self.click_sound_path = find_app_asset("click.wav")
        self._sound_cache = {}
        self._pygame_mixer_ok = False
        try:
            import pygame.mixer
            # On Windows, SDL2 needs directsound or winmm — tell it explicitly
            if sys.platform == "win32":
                os.environ.setdefault("SDL_AUDIODRIVER", "directsound")
            pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=128)
            pygame.mixer.init()
            self._pygame_mixer_ok = True
            if os.path.exists(self.click_sound_path):
                self._sound_cache[self.click_sound_path] = pygame.mixer.Sound(self.click_sound_path)
        except Exception as e:
            print(f"[sound] pygame init failed, using fallback: {e}")

        self.start_time      = int(time.time())
        self.rpc_last_update = 0
        self.rpc             = None
        self._last_sound_time = 0.0

        # ---------- CSS ----------
        css = """
        window, headerbar, .titlebar {
            background-color: #151515;
            color: #dddddd;
            border: none;
        }
        decoration {
            background-color: #151515;
            border: none;
            box-shadow: none;
        }
        .flesh-picture { border-radius: 12px; }
        @keyframes squish-anim {
            0%   { transform: scale(1.0); }
            40%  { transform: scale(0.94); }
            100% { transform: scale(1.0); }
        }
        .flesh-picture.squish {
            animation: squish-anim 100ms ease-out forwards;
        }
        .upgrade-row    { padding: 6px; }
        .achievement-row { padding: 4px; }
        .badge-unlocked { color: #a6e3a1; }
        .badge-locked   { color: #f38ba8; }
        label { color: #dddddd; }
        scrolledwindow, viewport, box { background-color: #151515; }
        notebook { background-color: #151515; }
        notebook > header {
            background-color: #1a1a1a;
            border-bottom: 1px solid #333;
        }
        notebook > header > tabs > tab {
            background-color: #1a1a1a;
            color: #aaaaaa;
            padding: 4px 12px;
            border: none;
        }
        notebook > header > tabs > tab:checked {
            background-color: #252525;
            color: #ffffff;
            border-bottom: 2px solid #4a9eff;
        }
        button, button * {
            background-color: #252525;
            background-image: none;
            color: #dddddd;
            border: 1px solid #333333;
            border-radius: 4px;
            padding: 4px 8px;
            box-shadow: none;
            text-shadow: none;
        }
        button:hover, button:hover * {
            background-color: #2e2e2e;
            background-image: none;
            border-color: #404040;
        }
        button:active, button:active * {
            background-color: #1e1e1e;
            background-image: none;
        }
        button.suggested-action, button.suggested-action * {
            background-color: #1c5a8a;
            background-image: none;
            border-color: #1c5a8a;
            color: #ffffff;
        }
        button.suggested-action:hover, button.suggested-action:hover * {
            background-color: #1c6ea4;
            background-image: none;
        }
        textview, textview > text { background-color: #1a1a1a; color: #dddddd; }
        entry {
            background-color: #1a1a1a;
            color: #dddddd;
            border: 1px solid #333333;
            border-radius: 4px;
        }
        spinbutton { background-color: #1a1a1a; color: #dddddd; border: 1px solid #333333; }
        paned { background-color: #151515; }
        paned > separator { background-color: #2a2a2a; min-width: 1px; min-height: 1px; }
        scrolledwindow { border: none; outline: none; }
        scrolledwindow undershoot, scrolledwindow overshoot { background: none; }
        frame { border: none; outline: none; }
        frame > border { border: none; }
        grid { background-color: #151515; border: none; }
        notebook > stack { border: none; background-color: #151515; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # load mods before building UI so they can register currencies/upgrades/textures
        self.load_mods()
        self.build_ui()
        self.update_labels()

        GLib.timeout_add(1000, self.on_timer_tick)

        if RPC_AVAILABLE and self.settings.get("enable_rpc"):
            self.init_rpc()
            GLib.timeout_add(2000, self.tick_rpc_update)

    # ---------- CURRENCY HELPERS ----------

    def get_currency(self, registry_name: str) -> float:
        return float(self.state["currencies"].get(registry_name, 0.0))

    def set_currency(self, registry_name: str, value: float):
        self.state["currencies"][registry_name] = max(0.0, float(value))
        save_json(STATE_FILE, self.state)
        if registry_name == "flesh":
            save_legacy_counter(self.state["currencies"][registry_name])

    def add_currency(self, registry_name: str, amount: float):
        self.set_currency(registry_name, self.get_currency(registry_name) + amount)

    # legacy property for callers that need the real vanilla flesh amount
    @property
    def flesh(self) -> float:
        return self.get_currency("flesh")

    # ---------- UPGRADE HELPERS ----------

    def get_upgrade_count(self, uid: str) -> int:
        return int(self.state["upgrades_owned"].get(uid, 0))

    def set_upgrade_count(self, uid: str, value: int):
        self.state["upgrades_owned"][uid] = int(value)
        save_json(STATE_FILE, self.state)

    def total_upgrades_owned(self) -> int:
        return sum(self.state["upgrades_owned"].values())

    def get_upgrade_cost(self, uid: str, owned: int) -> float:
        u = self.upgrades[uid]
        return u["base_cost"] * (u["cost_mult"] ** owned)

    def _get_effects(self, uid: str) -> list:
        """Return currency_effects list; falls back to legacy fps/fpc keys."""
        u = self.upgrades[uid]
        if "currency_effects" in u:
            return u["currency_effects"]
        effects = []
        fpc = u.get("fpc", 0.0)
        fps = u.get("fps", 0.0)
        if fpc or fps:
            effects.append({"currency": "flesh", "cpc": fpc, "cps": fps, "on_buy": 0.0})
        return effects

    def compute_cps(self, currency: str) -> float:
        """Total per-second gain for a currency from all owned upgrades."""
        total = 0.0
        for uid in self.upgrades:
            count = self.get_upgrade_count(uid)
            if not count:
                continue
            for effect in self._get_effects(uid):
                if effect.get("currency") == currency:
                    total += effect.get("cps", 0.0) * count
        return total

    def compute_cpc(self, currency: str) -> float:
        """Total per-click gain for a currency from all owned upgrades."""
        total = 0.0
        for uid in self.upgrades:
            count = self.get_upgrade_count(uid)
            if not count:
                continue
            for effect in self._get_effects(uid):
                if effect.get("currency") == currency:
                    total += effect.get("cpc", 0.0) * count
        return total

    def effective_fpc(self) -> float:
        base = self.state.get("flesh_per_click", 1.0)
        return base + self.compute_cpc(self.primary_currency)

    def on_filter_clicked(self, button, category_key):
        self.current_filter = category_key
        self._apply_upgrade_visibility()

    # ---------- DISCORD RPC ----------

    def init_rpc(self):
        try:
            self.rpc = Presence(RPC_CLIENT_ID)
            self.rpc.connect()
        except Exception:
            self.rpc = None

    def tick_rpc_update(self):
        if not self.rpc:
            return True
        now = time.time()
        if now - self.rpc_last_update < 10:
            return True
        self.rpc_last_update = now
        try:
            self.rpc.update(
                state="Playing Fleshfetch",
                details="Clicking the flesh",
                large_image="flesh",
                large_text="Flesh Clicker",
                start=self.start_time,
            )
        except Exception:
            pass
        return True

    # ---------- ACHIEVEMENT SOURCES ----------

    def _normalize_achievement_sources(self):
        dirty = False

        for key, default in DEFAULT_ACHIEVEMENTS.items():
            data = self.achievements.get(key)
            if not isinstance(data, dict):
                data = dict(default)
                self.achievements[key] = data
                dirty = True
            for field, value in default.items():
                if field not in data:
                    data[field] = value
                    dirty = True
            if data.get("source") != "builtin":
                data["source"] = "builtin"
                dirty = True
            if "mod_id" in data:
                data.pop("mod_id", None)
                dirty = True
            if "mod_name" in data:
                data.pop("mod_name", None)
                dirty = True

        for key, data in list(self.achievements.items()):
            if key in DEFAULT_ACHIEVEMENTS:
                continue
            if not isinstance(data, dict):
                self.achievements.pop(key, None)
                dirty = True
                continue
            if data.get("source") != "mod":
                data["source"] = "mod"
                dirty = True
            if not data.get("mod_id"):
                data["mod_id"] = "__unknown__"
                dirty = True
            if not data.get("mod_name"):
                data["mod_name"] = "Unknown mod"
                dirty = True
            if "unlocked" not in data:
                data["unlocked"] = False
                dirty = True

        if dirty:
            save_json(ACHIEVEMENTS_FILE, self.achievements)

    def _achievement_is_active(self, key: str, data: dict) -> bool:
        if data.get("source") != "mod":
            return True
        mod_id = data.get("mod_id")
        if not mod_id:
            return False
        current = getattr(self, "_current_mod_info", None)
        if current is not None and current.get("id") == mod_id:
            return True
        return mod_id in self.loaded_mod_ids

    def _get_mod_info(self, entry: str, mod_dir: str, manifest: dict) -> dict:
        mod_name = manifest.get("name") or manifest.get("title") or entry
        return {
            "id": entry,
            "name": str(mod_name),
            "version": str(manifest.get("version") or ""),
            "author": str(manifest.get("author") or ""),
            "description": str(manifest.get("description") or ""),
            "path": mod_dir,
        }

    def _read_mod_enabled(self, mod_dir: str):
        enabled_path = os.path.join(mod_dir, "enabled.txt")
        if not os.path.exists(enabled_path):
            try:
                with open(enabled_path, "w", encoding="utf-8") as f:
                    f.write("true\n")
            except Exception:
                pass

        try:
            with open(enabled_path, "r", encoding="utf-8") as f:
                text = f.read().strip().lower()
        except Exception:
            text = "true"

        return "false" not in text, enabled_path

    def _write_mod_enabled(self, mod_info: dict, enabled: bool):
        with open(mod_info["enabled_path"], "w", encoding="utf-8") as f:
            f.write("true\n" if enabled else "false\n")
        mod_info["enabled"] = enabled

    # ---------- MOD LOADING ----------

    def load_mods(self):
        """Load folder-based mods.

        Each mod is a subfolder containing:
            mod.py          — must define register(game)
            manifest.json   — optional metadata (name, version, description)
            assets/         — optional assets folder

        Inside mod.py, MOD_DIR is pre-set to the mod's folder path so you can
        reference assets like:  os.path.join(MOD_DIR, "assets", "custom.png")
        """
        self.installed_mods = []
        self.loaded_mod_ids = set()
        for mods_root in (SYSTEM_MODS_DIR, USER_MODS_DIR):
            if not os.path.isdir(mods_root):
                continue
            for entry in sorted(os.listdir(mods_root)):
                mod_dir = os.path.join(mods_root, entry)
                if not os.path.isdir(mod_dir):
                    continue

                mod_py = os.path.join(mod_dir, "mod.py")
                if not os.path.exists(mod_py):
                    continue

                manifest = {}
                manifest_path = os.path.join(mod_dir, "manifest.json")
                if os.path.exists(manifest_path):
                    try:
                        with open(manifest_path, "r", encoding="utf-8") as f:
                            manifest = json.load(f)
                    except Exception:
                        pass

                mod_info = self._get_mod_info(entry, mod_dir, manifest)
                enabled, enabled_path = self._read_mod_enabled(mod_dir)
                mod_info["enabled"] = enabled
                mod_info["enabled_path"] = enabled_path
                mod_info["root"] = mods_root
                if not enabled:
                    self.installed_mods.append(mod_info)
                    continue

                self._current_mod_info = mod_info
                try:
                    loader = importlib.machinery.SourceFileLoader(entry, mod_py)
                    spec   = importlib.util.spec_from_loader(loader.name, loader)
                    mod    = importlib.util.module_from_spec(spec)
                    mod.MOD_DIR  = mod_dir
                    mod.MANIFEST = manifest
                    loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(self)
                    self.loaded_mod_ids.add(mod_info["id"])
                    self.installed_mods.append(mod_info)
                except Exception as e:
                    print(f"[mods] Failed to load '{entry}': {e}")
                finally:
                    self._current_mod_info = None

        self.refresh_upgrades_ui()
        self.refresh_achievements_ui()

    # ---------- MOD API ----------

    def register_currency(self, registry_name: str, display_name: str):
        """Register a new currency. Safe to call if it already exists."""
        if registry_name not in self.currencies:
            self.currencies[registry_name] = {"display_name": display_name}
        if registry_name not in self.state["currencies"]:
            self.state["currencies"][registry_name] = 0.0

    def register_upgrade(self, uid: str, data: dict):
        """Add or update an upgrade in the registry."""
        if uid in self.upgrades:
            self.upgrades[uid].update(data)
        else:
            self.upgrades[uid] = data

    def register_achievement(self, key: str, data: dict):
        """Add or update an achievement."""
        existing = self.achievements.get(key, {})
        if not isinstance(existing, dict):
            existing = {}

        merged = dict(existing)
        merged.update(dict(data))
        if "unlocked" not in data:
            merged["unlocked"] = bool(existing.get("unlocked", False))

        mod_info = self._current_mod_info
        if mod_info is not None:
            merged["source"] = "mod"
            merged["mod_id"] = mod_info["id"]
            merged["mod_name"] = mod_info["name"]
        elif key in DEFAULT_ACHIEVEMENTS:
            merged["source"] = "builtin"
            merged.pop("mod_id", None)
            merged.pop("mod_name", None)
        else:
            merged.setdefault("source", "mod")
            merged.setdefault("mod_id", "__unknown__")
            merged.setdefault("mod_name", "Unknown mod")

        self.achievements[key] = merged
        save_json(ACHIEVEMENTS_FILE, self.achievements)

    def set_flesh_image(self, path: str):
        """Override the clickable image. Call from register() before build_ui runs."""
        self.flesh_image_path = path

    def set_click_sound(self, path: str):
        """Override the click sound. Must be a .wav file.
        Can be called at any time — the new sound takes effect on the next click.

        Example:
            game.set_click_sound(os.path.join(MOD_DIR, "assets", "pop.wav"))
        """
        self.click_sound_path = path
        # pre-load into pygame cache if available so first click has no delay
        if self._pygame_mixer_ok and os.path.exists(path):
            try:
                import pygame.mixer
                if path not in self._sound_cache:
                    self._sound_cache[path] = pygame.mixer.Sound(path)
            except Exception:
                pass

    def add_tab(self, tab_id: str, label: str, page_box=None):
        """Add a custom tab to the notebook.

        tab_id   — unique string ID used to reference this tab in add_tab_button()
        label    — text shown on the tab
        page_box — optional Gtk.Box to use as the page. If None, a plain vertical
                   Box with 6px spacing is created and returned so you can append
                   widgets to it.

        Returns the Gtk.Box that is the tab's page, so you can populate it:

            box = game.add_tab("mystats", "My Stats")
            box.append(Gtk.Label(label="Hello from my mod!"))

        Can be called before or after build_ui — if the UI is already built the
        tab is added immediately, otherwise it is queued and added at build time.
        """
        if page_box is None:
            page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            page_box.set_margin_top(4)
            page_box.set_margin_bottom(4)
            page_box.set_margin_start(4)
            page_box.set_margin_end(4)

        if hasattr(self, "notebook"):
            # UI already built — add immediately
            self.notebook.append_page(page_box, Gtk.Label(label=label))
            self._tab_pages[tab_id] = page_box
            # flush any buttons that were queued for this tab
            for btn_label, callback in self._pending_buttons.pop(tab_id, []):
                btn = Gtk.Button(label=btn_label)
                btn.connect("clicked", lambda b, cb=callback: cb(b))
                page_box.append(btn)
        else:
            self._pending_tabs.append((tab_id, label, page_box))

        return page_box

    def add_tab_button(self, tab_id: str, label: str, callback):
        """Add a button to an existing tab (built-in or mod-created).

        tab_id   — ID of the target tab. Built-in IDs are:
                   "upgrades", "achievements", "leaderboard", "settings"
                   Mod tab IDs are whatever string you passed to add_tab().
        label    — button text
        callback — function called when clicked. Receives the Gtk.Button as its
                   only argument.

        Can be called before or after build_ui — if the tab already exists the
        button is added immediately, otherwise it is queued.

        Example:
            def my_callback(button):
                print("clicked!")

            game.add_tab_button("settings", "Reset Stats", my_callback)
        """
        page = self._tab_pages.get(tab_id)
        if page is not None:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda b, cb=callback: cb(b))
            page.append(btn)
        else:
            self._pending_buttons.setdefault(tab_id, []).append((label, callback))

    def add_button(self, tab_id: str, label: str, callback):
        """Compatibility alias for add_tab_button()."""
        self.add_tab_button(tab_id, label, callback)

    def set_click_sound(self, path: str):
        """Override the click sound. Call from register() at any time.
        Path must point to a .wav file."""
        self.click_sound_path = path

    def disable_vanilla_achievements(self):
        """Remove all built-in achievements. Mod achievements registered after
        this call are unaffected."""
        for key in list(DEFAULT_ACHIEVEMENTS.keys()):
            self.achievements.pop(key, None)

    def disable_vanilla_upgrades(self):
        """Remove all built-in upgrades. Mod upgrades registered after
        this call are unaffected."""
        for key in list(BASE_UPGRADES.keys()):
            self.upgrades.pop(key, None)

    def disable_vanilla(self, primary_currency: str):
        """Remove all built-in upgrades, achievements, and the flesh currency UI,
        and set a new primary currency.

        Call this before registering your own currency/upgrades/achievements.
        The primary_currency you pass must be registered with register_currency()
        either before or after this call — it just needs to exist by the time
        the UI is built.

        Example:
            def register(game):
                game.register_currency("souls", "Souls")
                game.disable_vanilla("souls")
                game.register_upgrade("soul_harvester", { ... })
        """
        self.disable_vanilla_upgrades()
        self.disable_vanilla_achievements()

        # hide vanilla flesh while keeping the saved amount intact for later
        self.currencies.pop("flesh", None)

        # point the primary currency at the mod's replacement
        self.primary_currency = primary_currency

    # ---------- UI BUILD ----------

    def build_ui(self):
        root = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        root.set_wide_handle(True)
        self.set_child(root)

        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_margin_top(12)
        left_box.set_margin_bottom(12)
        left_box.set_margin_start(12)
        left_box.set_margin_end(6)

        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.add_css_class("flesh-picture")
        self.picture.set_vexpand(True)
        self.picture.set_hexpand(True)

        click_gesture = Gtk.GestureClick()
        click_gesture.connect("released", self.on_click)
        self.picture.add_controller(click_gesture)

        left_box.append(self.picture)
        self.load_flesh_image()

        # dynamic stats area — rebuilt every tick
        self.stats_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left_box.append(self.stats_box)

        root.set_start_child(left_box)

        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_box.set_margin_top(12)
        right_box.set_margin_bottom(12)
        right_box.set_margin_start(6)
        right_box.set_margin_end(12)

        self.notebook = Gtk.Notebook()
        right_box.append(self.notebook)

        self.upgrades_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_upgrades_page()
        self.notebook.append_page(self.upgrades_page, Gtk.Label(label="Upgrades"))
        self._tab_pages["upgrades"] = self.upgrades_page

        self.achievements_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_achievements_page()
        self.notebook.append_page(self.achievements_page, Gtk.Label(label="Achievements"))
        self._tab_pages["achievements"] = self.achievements_page

        self.leaderboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_leaderboard_page()
        self.notebook.append_page(self.leaderboard_page, Gtk.Label(label="Leaderboard"))
        self._tab_pages["leaderboard"] = self.leaderboard_page

        self.settings_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_settings_page()
        self.notebook.append_page(self.settings_page, Gtk.Label(label="Settings"))
        self._tab_pages["settings"] = self.settings_page

        self.console_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.build_console_page()
        self.notebook.append_page(self.console_page, Gtk.Label(label="Console"))
        self._tab_pages["console"] = self.console_page

        self.stats_tab_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.build_stats_tab_page()
        self.notebook.append_page(self.stats_tab_page, Gtk.Label(label="Stats"))
        self._tab_pages["stats"] = self.stats_tab_page

        # flush mod tabs queued before build_ui ran
        for tab_id, label, page_box in self._pending_tabs:
            self.notebook.append_page(page_box, Gtk.Label(label=label))
            self._tab_pages[tab_id] = page_box

        # flush mod buttons queued before build_ui ran
        for tab_id, buttons in self._pending_buttons.items():
            page = self._tab_pages.get(tab_id)
            if page is None:
                continue
            for btn_label, callback in buttons:
                btn = Gtk.Button(label=btn_label)
                btn.connect("clicked", lambda b, cb=callback: cb(b))
                page.append(btn)

        self.refresh_upgrades_ui()
        self.refresh_achievements_ui()
        root.set_end_child(right_box)

    def build_upgrades_page(self):
        self.upgrades_page.set_margin_top(4)
        self.upgrades_page.set_margin_bottom(4)
        self.upgrades_page.set_margin_start(4)
        self.upgrades_page.set_margin_end(4)

        # Search bar
        self.upgrade_search = Gtk.SearchEntry()
        self.upgrade_search.set_placeholder_text("Search upgrades\u2026")
        self.upgrade_search.connect("search-changed", self._on_upgrade_search_changed)
        self.upgrades_page.append(self.upgrade_search)

        # Filter buttons
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.upgrade_filter_buttons = {}

        def make_filter_button(label, key):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self.on_filter_clicked, key)
            self.upgrade_filter_buttons[key] = btn
            filter_box.append(btn)

        make_filter_button("All",   "all")
        make_filter_button("Click", "click")
        make_filter_button("Auto",  "auto")

        self.upgrades_page.append(filter_box)

        self.upgrades_listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.current_filter   = "all"
        self._upgrade_search_text = ""

        # Keep a reference to the ScrolledWindow so we never lose scroll position
        self._upgrades_scroll = Gtk.ScrolledWindow()
        self._upgrades_scroll.set_child(self.upgrades_listbox)
        self._upgrades_scroll.set_vexpand(True)
        self.upgrades_page.append(self._upgrades_scroll)

        # Pre-build all upgrade rows once -- stored in _upgrade_rows by uid
        self._upgrade_rows = {}
        self._build_all_upgrade_rows()

    def build_achievements_page(self):
        self.achievements_page.set_margin_top(4)
        self.achievements_page.set_margin_bottom(4)
        self.achievements_page.set_margin_start(4)
        self.achievements_page.set_margin_end(4)

        self.achievements_listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.achievements_listbox)
        scroll.set_vexpand(True)
        self.achievements_page.append(scroll)

    def build_settings_page(self):
        self.settings_page.set_margin_top(4)
        self.settings_page.set_margin_bottom(4)
        self.settings_page.set_margin_start(4)
        self.settings_page.set_margin_end(4)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_hexpand(False)
        self.settings_page.append(grid)
        row = 0

        rpc_label = Gtk.Label(label="Enable Discord RPC", xalign=0)
        grid.attach(rpc_label, 0, row, 1, 1)
        self.rpc_switch = Gtk.Switch()
        self.rpc_switch.set_halign(Gtk.Align.START)
        self.rpc_switch.set_hexpand(False)
        self.rpc_switch.set_active(bool(self.settings.get("enable_rpc")))
        self.rpc_switch.connect("notify::active", self.on_settings_changed)
        grid.attach(self.rpc_switch, 1, row, 1, 1)
        row += 1

        squish_label = Gtk.Label(label="Squish duration (ms)", xalign=0)
        grid.attach(squish_label, 0, row, 1, 1)
        self.squish_spin = Gtk.SpinButton.new_with_range(20, 300, 10)
        self.squish_spin.set_value(int(self.settings.get("squish_ms", 100)))
        self.squish_spin.connect("value-changed", self.on_settings_changed)
        grid.attach(self.squish_spin, 1, row, 1, 1)
        row += 1

        sound_label = Gtk.Label(label="Play click sound", xalign=0)
        grid.attach(sound_label, 0, row, 1, 1)
        self.sound_switch = Gtk.Switch()
        self.sound_switch.set_halign(Gtk.Align.START)
        self.sound_switch.set_hexpand(False)
        self.sound_switch.set_active(bool(self.settings.get("play_click_sound")))
        self.sound_switch.connect("notify::active", self.on_settings_changed)
        grid.attach(self.sound_switch, 1, row, 1, 1)
        row += 1

        volume_label = Gtk.Label(label="Click sound volume", xalign=0)
        grid.attach(volume_label, 0, row, 1, 1)
        self.volume_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume_slider.set_value(int(self.settings.get("click_sound_volume", DEFAULT_SETTINGS["click_sound_volume"])))
        self.volume_slider.set_hexpand(False)
        self.volume_slider.set_size_request(200, -1)
        self.volume_slider.set_draw_value(True)
        self.volume_slider.connect("value-changed", self.on_settings_changed)
        grid.attach(self.volume_slider, 1, row, 1, 1)
        row += 1

        leaderboard_button = Gtk.Button(label="Add leaderboard entry")
        leaderboard_button.connect("clicked", self.on_add_leaderboard_clicked)
        grid.attach(leaderboard_button, 0, row, 2, 1)
        row += 1

        self.settings_info_label = Gtk.Label(label="", xalign=0)
        self.settings_page.append(self.settings_info_label)
        self.build_mod_settings_list()

    def build_mod_settings_list(self):
        mods_label = Gtk.Label(label="Installed mods", xalign=0)
        mods_label.add_css_class("badge-unlocked")
        self.settings_page.append(mods_label)

        mods_scroll = Gtk.ScrolledWindow()
        mods_scroll.set_vexpand(True)
        mods_scroll.set_hexpand(True)
        try:
            mods_scroll.set_min_content_height(160)
        except Exception:
            pass

        self.mods_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        mods_scroll.set_child(self.mods_list_box)
        self.settings_page.append(mods_scroll)
        self.refresh_mod_settings_list()

    def refresh_mod_settings_list(self):
        if not hasattr(self, "mods_list_box"):
            return
        self.clear_box_children(self.mods_list_box)

        if not self.installed_mods:
            self.mods_list_box.append(Gtk.Label(label="No installed mods found.", xalign=0))
            return

        for mod_info in self.installed_mods:
            status = "Enabled" if mod_info.get("enabled") else "Disabled"
            expander = Gtk.Expander(label=f"{mod_info.get('name') or mod_info['id']} ({status})")

            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            details.set_margin_top(4)
            details.set_margin_bottom(4)
            details.set_margin_start(12)
            details.set_margin_end(4)

            meta_lines = [
                ("Author", mod_info.get("author") or "Unknown"),
                ("Version", mod_info.get("version") or "Unknown"),
                ("Description", mod_info.get("description") or "No description provided."),
            ]
            for label, value in meta_lines:
                lbl = Gtk.Label(label=f"{label}: {value}", xalign=0)
                lbl.set_wrap(True)
                details.append(lbl)

            button_label = "Disable" if mod_info.get("enabled") else "Enable"
            toggle_btn = Gtk.Button(label=button_label)
            toggle_btn.connect("clicked", self.on_mod_toggle_clicked, mod_info)
            details.append(toggle_btn)

            expander.set_child(details)
            self.mods_list_box.append(expander)

    def on_mod_toggle_clicked(self, button, mod_info: dict):
        new_enabled = not mod_info.get("enabled", True)
        try:
            self._write_mod_enabled(mod_info, new_enabled)
        except Exception as e:
            self.settings_info_label.set_text(f"Failed to update mod '{mod_info.get('name', mod_info['id'])}': {e}")
            return

        state = "enabled" if new_enabled else "disabled"
        self.settings_info_label.set_text(
            f"Mod '{mod_info.get('name', mod_info['id'])}' {state}. Restart Fleshfetch to apply."
        )
        self.refresh_mod_settings_list()

    def build_console_page(self):
        self.console_page.set_margin_top(4)
        self.console_page.set_margin_bottom(4)
        self.console_page.set_margin_start(4)
        self.console_page.set_margin_end(4)

        # toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", self._on_console_clear)
        toolbar.append(clear_btn)
        self.console_page.append(toolbar)

        # scrolled textview
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self.console_page.append(scrolled)

        self._console_textview = Gtk.TextView()
        self._console_textview.set_editable(False)
        self._console_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        try:
            self._console_textview.set_monospace(True)
        except Exception:
            pass
        scrolled.set_child(self._console_textview)
        self._console_buffer = self._console_textview.get_buffer()

        # hook into the tee streams
        _stdout_tee._callbacks.append(self._console_append)
        _stderr_tee._callbacks.append(self._console_append)
        for text in _stdout_tee._history + _stderr_tee._history:
            if text:
                self._console_append(text)

    def _console_append(self, text: str):
        """Append text to the console buffer, thread-safely via GLib.idle_add."""
        def _do_append():
            end = self._console_buffer.get_end_iter()
            self._console_buffer.insert(end, text, -1)
            # move the insert mark to the new end so scroll_mark_onscreen goes to bottom
            new_end = self._console_buffer.get_end_iter()
            self._console_buffer.place_cursor(new_end)
            def _scroll():
                mark = self._console_buffer.get_insert()
                self._console_textview.scroll_mark_onscreen(mark)
                return False
            GLib.idle_add(_scroll)
            return False
        GLib.idle_add(_do_append)

    def _on_console_clear(self, button):
        self._console_buffer.set_text("", -1)

    def build_stats_tab_page(self):
        self.stats_tab_page.set_margin_top(4)
        self.stats_tab_page.set_margin_bottom(4)
        self.stats_tab_page.set_margin_start(4)
        self.stats_tab_page.set_margin_end(4)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self.stats_tab_page.append(scrolled)

        self._stats_tab_textview = Gtk.TextView()
        self._stats_tab_textview.set_editable(False)
        self._stats_tab_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        try:
            self._stats_tab_textview.set_monospace(True)
        except Exception:
            pass
        scrolled.set_child(self._stats_tab_textview)
        self._stats_tab_buffer = self._stats_tab_textview.get_buffer()

    def refresh_stats_tab(self):
        if not hasattr(self, "_stats_tab_buffer"):
            return

        lines = []

        # ── Currencies ──────────────────────────────────────────────────────
        lines.append("=== Currencies ===")
        for reg_name, cur_data in self.currencies.items():
            amount = self.get_currency(reg_name)
            display = cur_data.get("display_name", reg_name)
            cps = self.compute_cps(reg_name)
            if reg_name == self.primary_currency:
                cpc = self.effective_fpc()
            else:
                cpc = self.compute_cpc(reg_name)
            lines.append(
                f"{display}: {int(amount)}  "
                f"(per second: {cps:.2f}, per click: {cpc:.2f})"
            )

        # ── Upgrades owned ──────────────────────────────────────────────────
        lines.append("")
        lines.append("=== Upgrades ===")
        any_owned = False
        for uid, u in self.upgrades.items():
            count = self.get_upgrade_count(uid)
            if count > 0:
                lines.append(f"{u.get('name', uid)}: {count}")
                any_owned = True
        if not any_owned:
            lines.append("(none owned yet)")

        self._stats_tab_buffer.set_text("\n".join(lines), -1)

    def build_leaderboard_page(self):
        self.leaderboard_page.set_margin_top(4)
        self.leaderboard_page.set_margin_bottom(4)
        self.leaderboard_page.set_margin_start(4)
        self.leaderboard_page.set_margin_end(4)

        outer    = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.append(controls)
        self.leaderboard_page.append(outer)

        refresh_btn = Gtk.Button(label="Refresh leaderboard")
        refresh_btn.connect("clicked", self.on_refresh_leaderboard_clicked)
        controls.append(refresh_btn)

        self.leaderboard_info_label = Gtk.Label(label="", xalign=0)
        outer.append(self.leaderboard_info_label)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        outer.append(scrolled)

        self.leaderboard_textview = Gtk.TextView()
        self.leaderboard_textview.set_editable(False)
        try:
            self.leaderboard_textview.set_monospace(True)
        except Exception:
            pass
        scrolled.set_child(self.leaderboard_textview)
        self.load_leaderboard()

    def update_leaderboard_view(self, rows):
        buffer = self.leaderboard_textview.get_buffer()
        if not rows:
            buffer.set_text("No leaderboard entries yet.", -1)
            return
        header    = f"{'Username':<20} {'Flesh':>10}  {'Timestamp':<32}\n"
        separator = "-" * 64 + "\n"
        lines     = [header, separator]
        for row in rows:
            username = str(row.get("username") or "?")
            flesh    = row.get("flesh_amount") or row.get("flesh")
            try:
                flesh_str = str(int(flesh))
            except Exception:
                flesh_str = str(flesh)
            ts = row.get("last_update") or row.get("created_at") or ""
            lines.append(f"{username:<20} {flesh_str:>10}  {ts:<32}\n")
        buffer.set_text("".join(lines), -1)

    def load_leaderboard(self):
        if not hasattr(self, "leaderboard_textview"):
            return
        ok, err, rows = fetch_leaderboard_entries()
        if not ok:
            if hasattr(self, "leaderboard_info_label"):
                self.leaderboard_info_label.set_text(f"Error loading leaderboard: {err}")
            return
        if hasattr(self, "leaderboard_info_label"):
            self.leaderboard_info_label.set_text(f"Loaded {len(rows)} entries.")
        self.update_leaderboard_view(rows)

    def on_refresh_leaderboard_clicked(self, button):
        self.load_leaderboard()

    def on_add_leaderboard_clicked(self, button):
        username     = get_default_username()
        flesh_amount = int(self.flesh)
        ok, info = submit_leaderboard_entry(username, flesh_amount)
        if ok:
            msg = f"Submitted leaderboard entry as '{username}' with {flesh_amount} flesh."
            if hasattr(self, "leaderboard_textview"):
                self.load_leaderboard()
        else:
            msg = f"Failed to submit leaderboard entry: {info}"
        if hasattr(self, "settings_info_label"):
            self.settings_info_label.set_text(msg)
        else:
            print(msg)

    # ---------- IMAGE ----------

    def load_flesh_image(self):
        if not hasattr(self, "picture"):
            return  # UI not built yet — build_ui() will call this again
        try:
            texture = Gdk.Texture.new_from_filename(self.flesh_image_path)
            self.picture.set_paintable(texture)
        except Exception as e:
            print(f"Failed to load image '{self.flesh_image_path}':", e)

    # ---------- SOUND ----------

    def play_click_sound(self):
        """Play the click sound if enabled in settings."""
        if not self.settings.get("play_click_sound"):
            return
        path = self.click_sound_path
        if not os.path.exists(path):
            return

        volume = max(0, min(100, int(self.settings.get("click_sound_volume", DEFAULT_SETTINGS["click_sound_volume"]))))

        # --- pygame path (fast, in-memory, no process spawning) ---
        if self._pygame_mixer_ok:
            try:
                import pygame.mixer
                if path not in self._sound_cache:
                    self._sound_cache[path] = pygame.mixer.Sound(path)
                snd = self._sound_cache[path]
                snd.set_volume(volume / 100.0)
                snd.play()
                print(f"[sound] played via pygame: {path}")
                return
            except Exception as e:
                print(f"[sound] pygame failed: {e}")

        # --- fallback: throttle to avoid process backlog ---
        now = time.monotonic()
        if now - self._last_sound_time < 0.08:
            return
        self._last_sound_time = now

        try:
            if sys.platform == "win32":
                import winsound
                if volume == 100:
                    winsound.PlaySound(
                        path,
                        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                    )
                else:
                    import wave, struct, tempfile
                    with wave.open(path, "rb") as wf:
                        params = wf.getparams()
                        frames = wf.readframes(params.nframes)
                    factor = volume / 100.0
                    sw = params.sampwidth
                    fmt = {1: "b", 2: "h", 4: "i"}.get(sw)
                    if fmt:
                        n = len(frames) // sw
                        samples = struct.unpack_from(f"{n}{fmt}", frames)
                        scaled  = struct.pack(f"{n}{fmt}", *(max(-32768, min(32767, int(s * factor))) for s in samples))
                    else:
                        scaled = frames
                    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                    with wave.open(tmp.name, "wb") as wf:
                        wf.setparams(params)
                        wf.writeframes(scaled)
                    winsound.PlaySound(
                        tmp.name,
                        winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                    )
                    def _cleanup(f=tmp.name):
                        try:
                            os.unlink(f)
                        except Exception:
                            pass
                        return False
                    GLib.timeout_add(2000, _cleanup)
            else:
                import subprocess
                vol_pa = int(65536 * volume / 100)
                for player, args in [
                    ("paplay", ["--volume", str(vol_pa), path]),
                    ("aplay",  [path]),
                ]:
                    try:
                        subprocess.Popen(
                            [player] + args,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        break
                    except FileNotFoundError:
                        continue
        except Exception as e:
            print(f"Failed to play click sound '{path}':", e)

    # ---------- UI HELPERS ----------

    def clear_box_children(self, box: Gtk.Box):
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def check_requires(self, uid: str) -> bool:
        """Return True if all requirements for an upgrade are satisfied.

        The ``requires`` field on an upgrade can be a single dict or a list
        of dicts.  Each dict may contain:

            upgrade  (str)  — another upgrade uid that must be owned
            count    (int)  — minimum number of that upgrade owned (default 1)
            currency (str)  — a currency registry name
            amount   (float)— minimum amount of that currency required (default 0)

        All conditions in a single dict must be satisfied (AND logic).
        If ``requires`` is a list, *any* one dict being satisfied is enough
        (OR logic between list items, AND within each item).

        Example — require lignification AND at least 500 wood::

            "requires": {"upgrade": "lignification", "currency": "wood", "amount": 500}

        Example — require either sapwood OR heartwood::

            "requires": [
                {"upgrade": "sapwood"},
                {"upgrade": "heartwood"},
            ]
        """
        u = self.upgrades.get(uid)
        if u is None:
            return False
        req = u.get("requires")
        if req is None:
            return True

        # normalise to a list of condition-dicts
        conditions = req if isinstance(req, list) else [req]

        for cond in conditions:
            ok = True
            needed_upgrade  = cond.get("upgrade")
            needed_count    = int(cond.get("count", 1))
            needed_currency = cond.get("currency")
            needed_amount   = float(cond.get("amount", 0))

            if needed_upgrade is not None:
                if self.get_upgrade_count(needed_upgrade) < needed_count:
                    ok = False
            if needed_currency is not None:
                if self.get_currency(needed_currency) < needed_amount:
                    ok = False

            if ok:
                return True  # at least one condition-set satisfied

        return False

    def _build_all_upgrade_rows(self):
        """Build every upgrade row once and append to the listbox.
        Rows are shown/hidden via set_visible() -- the DOM is never rebuilt."""
        if not hasattr(self, "upgrades_listbox"):
            return
        for uid, u in self.upgrades.items():
            if uid in self._upgrade_rows:
                continue  # already built
            self._build_upgrade_row(uid, u)

    def _build_upgrade_row(self, uid: str, u: dict):
        """Create the GTK widgets for a single upgrade and store references."""
        owned         = self.get_upgrade_count(uid)
        cost          = self.get_upgrade_cost(uid, owned)
        cost_currency = u.get("cost_currency", "flesh")
        cost_display  = self.currencies.get(cost_currency, {}).get("display_name", cost_currency)
        cat           = u.get("category") or u.get("type") or "misc"

        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row.add_css_class("upgrade-row")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        name_label = Gtk.Label(label=f"{u['name']} ({cat})", xalign=0)
        name_label.set_hexpand(True)
        top.append(name_label)

        cost_label = Gtk.Label(label=f"Cost: {int(cost)} {cost_display},", xalign=1)
        top.append(cost_label)
        owned_label = Gtk.Label(label=f"Owned: {owned}", xalign=1)
        top.append(owned_label)
        row.append(top)

        desc_label = Gtk.Label(label=u["desc"], xalign=0)
        desc_label.set_wrap(True)
        row.append(desc_label)

        btn = Gtk.Button(label="Buy")
        btn.connect("clicked", self.on_buy_upgrade_clicked, uid)
        row.append(btn)

        self.upgrades_listbox.append(row)
        self._upgrade_rows[uid] = {
            "row":         row,
            "cost_label":  cost_label,
            "owned_label": owned_label,
            "buy_btn":     btn,
            "category":    cat,
            "name":        u["name"].lower(),
            "desc":        u["desc"].lower(),
        }

    def _on_upgrade_search_changed(self, entry):
        self._upgrade_search_text = entry.get_text().lower()
        self._apply_upgrade_visibility()

    def _apply_upgrade_visibility(self):
        """Show/hide rows in-place. Updates filter button styles and cost labels.
        Never rebuilds widgets -- scroll position is always preserved."""
        if not hasattr(self, "_upgrade_rows"):
            return

        selected_filter = getattr(self, "current_filter", "all")
        search = getattr(self, "_upgrade_search_text", "")

        # Update filter button styles
        if hasattr(self, "upgrade_filter_buttons"):
            for key, btn in self.upgrade_filter_buttons.items():
                if key == selected_filter:
                    btn.add_css_class("suggested-action")
                else:
                    btn.remove_css_class("suggested-action")

        for uid, meta in self._upgrade_rows.items():
            u = self.upgrades.get(uid)
            if u is None:
                meta["row"].set_visible(False)
                continue

            # Category filter
            if selected_filter != "all" and meta["category"] != selected_filter:
                meta["row"].set_visible(False)
                continue

            # Requirements gate
            if not self.check_requires(uid):
                meta["row"].set_visible(False)
                continue

            # Search filter
            if search and search not in meta["name"] and search not in meta["desc"]:
                meta["row"].set_visible(False)
                continue

            # Row is visible -- refresh cost label in-place
            owned         = self.get_upgrade_count(uid)
            cost          = self.get_upgrade_cost(uid, owned)
            cost_currency = u.get("cost_currency", "flesh")
            cost_display  = self.currencies.get(cost_currency, {}).get("display_name", cost_currency)
            meta["cost_label"].set_text(f"Cost: {int(cost)} {cost_display},")
            meta["owned_label"].set_text(f"Owned: {owned}")
            meta["row"].set_visible(True)

    def refresh_upgrades_ui(self):
        """Public method kept for mod compatibility.
        Builds any newly registered rows then updates visibility in-place."""
        if not hasattr(self, "upgrades_listbox"):
            return
        # Build rows for any upgrades added since initial build (e.g. by mods)
        if hasattr(self, "_upgrade_rows"):
            self._build_all_upgrade_rows()
        self._apply_upgrade_visibility()

    def refresh_achievements_ui(self):
        if not hasattr(self, "achievements_listbox"):
            return
        self.clear_box_children(self.achievements_listbox)
        unlocked_any = False
        for key, data in self.achievements.items():
            if not data.get("unlocked", False):
                continue
            if not self._achievement_is_active(key, data):
                continue
            unlocked_any = True
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.add_css_class("achievement-row")
            name_label = Gtk.Label(label=data.get("name", key), xalign=0)
            desc_label = Gtk.Label(label=data.get("desc", ""),  xalign=0)
            desc_label.set_wrap(True)
            name_label.add_css_class("badge-unlocked")
            row.append(name_label)
            row.append(desc_label)
            self.achievements_listbox.append(row)
        if not unlocked_any:
            placeholder = Gtk.Label(label="No achievements unlocked yet.", xalign=0)
            self.achievements_listbox.append(placeholder)

    # ---------- TIMER / GAME LOOP ----------

    def on_timer_tick(self):
        for reg_name in self.currencies:
            cps = self.compute_cps(reg_name)
            if cps > 0:
                self.add_currency(reg_name, cps)
        self.update_labels()
        return True

    def update_labels(self):
        if not hasattr(self, "stats_box"):
            return
        self.clear_box_children(self.stats_box)
        for reg_name, cur_data in self.currencies.items():
            amount = self.get_currency(reg_name)
            # hide mod currencies until the player first obtains some
            if amount <= 0 and reg_name != "flesh":
                continue
            display = cur_data.get("display_name", reg_name)
            lbl = Gtk.Label(xalign=0)
            lbl.set_text(f"{display}: {int(amount)}")
            self.stats_box.append(lbl)
            cps = self.compute_cps(reg_name)
            if cps > 0:
                cps_lbl = Gtk.Label(xalign=0)
                cps_lbl.set_text(f"{display} per second: {cps:.1f}")
                self.stats_box.append(cps_lbl)
        self.refresh_stats_tab()

    # ---------- UPGRADE LOGIC ----------

    def on_buy_upgrade_clicked(self, button, uid: str):
        owned         = self.get_upgrade_count(uid)
        cost          = self.get_upgrade_cost(uid, owned)
        cost_currency = self.upgrades[uid].get("cost_currency", "flesh")

        if self.get_currency(cost_currency) < cost:
            return

        self.add_currency(cost_currency, -cost)
        self.set_upgrade_count(uid, owned + 1)

        # one-time on_buy currency grants
        for effect in self._get_effects(uid):
            on_buy = effect.get("on_buy", 0.0)
            if on_buy:
                self.add_currency(effect["currency"], on_buy)

        total = self.total_upgrades_owned()
        if total >= 1: self.unlock_achievement("first_upgrade")
        if total >= 5: self.unlock_achievement("five_upgrades")

        # Update cost labels and visibility in-place -- no rebuild, no scroll jump
        self._apply_upgrade_visibility()
        self.update_labels()

    def play_squish(self):
        self.picture.remove_css_class("squish")
        duration = int(self.settings.get("squish_ms", 100))

        def do_squish():
            self.picture.add_css_class("squish")
            GLib.timeout_add(duration, lambda: (self.picture.remove_css_class("squish"), False)[1])
            return False

        GLib.idle_add(do_squish)

    # ---------- CLICK HANDLER ----------

    def on_click(self, gesture, n_press, x, y):
        self.play_squish()
        self.play_click_sound()

        crit_count  = self.get_upgrade_count("crit_click")
        crit_chance = 0.05 * crit_count if crit_count > 0 else 0.0
        multiplier  = 2.0 if random.random() < crit_chance else 1.0

        # primary currency uses base fpc + upgrade cpc; others use only upgrade cpc
        for reg_name in self.currencies:
            if reg_name == self.primary_currency:
                gain = self.effective_fpc() * multiplier
            else:
                gain = self.compute_cpc(reg_name) * multiplier
            if gain:
                self.add_currency(reg_name, gain)

        self.state["total_clicks"] += 1
        save_json(STATE_FILE, self.state)

        clicks = self.state["total_clicks"]
        if clicks >= 1:   self.unlock_achievement("first_click")
        if clicks >= 10:  self.unlock_achievement("ten_clicks")
        if clicks >= 100: self.unlock_achievement("hundred_clicks")
        if self.flesh >= 100:  self.unlock_achievement("hundred_flesh")
        if self.flesh >= 1000: self.unlock_achievement("thousand_flesh")

        self.update_labels()

    # ---------- ACHIEVEMENTS ----------

    def unlock_achievement(self, key: str):
        if key not in self.achievements:
            return
        if not self._achievement_is_active(key, self.achievements[key]):
            return
        if not self.achievements[key].get("unlocked", False):
            self.achievements[key]["unlocked"] = True
            save_json(ACHIEVEMENTS_FILE, self.achievements)
            self.refresh_achievements_ui()

    # ---------- SETTINGS ----------

    def on_settings_changed(self, *args):
        self.settings["enable_rpc"]           = self.rpc_switch.get_active()
        self.settings["squish_ms"]            = int(self.squish_spin.get_value())
        self.settings["play_click_sound"]     = self.sound_switch.get_active()
        self.settings["click_sound_volume"]   = int(self.volume_slider.get_value())
        save_json(SETTINGS_FILE, self.settings)


class FleshApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.window = None

    def do_activate(self, *args):
        if not self.window:
            self.window = FleshClicker(self)
        self.window.present()


def main():
    app = FleshApp()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _handle_uncaught_exception(type(exc), exc, exc.__traceback__)
        sys.exit(1)
