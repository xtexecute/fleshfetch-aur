#!/usr/bin/env python3
import os
import json
import time
import random
import importlib.machinery
import importlib.util

import requests
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, Gdk, GLib

# ---------- OPTIONAL DISCORD RPC ----------
try:
    from pypresence import Presence
    RPC_AVAILABLE = True
except Exception:
    Presence = None
    RPC_AVAILABLE = False

APP_ID = "dev.xtexecute.fleshfetch"

# Base directory of the installed code (assets live here)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# XDG config directory for user-writable save data (for packaging / AUR)
XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
CONFIG_DIR = os.path.join(XDG_CONFIG_HOME, "fleshfetch")

# Ensure config dir exists so saves don't fail when installed system-wide
os.makedirs(CONFIG_DIR, exist_ok=True)

# New save locations (user-writable)
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
ACHIEVEMENTS_FILE = os.path.join(CONFIG_DIR, "achievements.json")
COUNTER_FILE = os.path.join(CONFIG_DIR, "flesh_counter.txt")

# Legacy file locations (pre-AUR, next to the script)
LEGACY_STATE_FILE = os.path.join(BASE_DIR, "state.json")
LEGACY_SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
LEGACY_ACHIEVEMENTS_FILE = os.path.join(BASE_DIR, "achievements.json")
LEGACY_COUNTER_FILE = os.path.join(BASE_DIR, "flesh_counter.txt")

# Mods: system-wide (next to code) and per-user (in config dir)
SYSTEM_MODS_DIR = os.path.join(BASE_DIR, "mods")
USER_MODS_DIR = os.path.join(CONFIG_DIR, "mods")

# Make sure user mods dir exists so players can drop files in
os.makedirs(USER_MODS_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "enable_rpc": False,
    "discord_client_id": "",
    "squish_ms": 100,
    "play_click_sound": False,
}

DEFAULT_STATE = {
    "flesh": 0.0,
    "flesh_per_click": 1.0,
    "upgrades_owned": {},
    "total_clicks": 0,
}

DEFAULT_ACHIEVEMENTS = {
    "first_click": {"name": "First Click", "desc": "Click the flesh at least once.", "unlocked": False},
    "ten_clicks": {"name": "Ten Clicks", "desc": "Click the flesh 10 times.", "unlocked": False},
    "hundred_clicks": {"name": "Hundred Clicks", "desc": "Click the flesh 100 times.", "unlocked": False},
    "first_upgrade": {"name": "First Upgrade", "desc": "Buy your first upgrade.", "unlocked": False},
    "five_upgrades": {"name": "Upgrade Collector", "desc": "Own at least 5 upgrades total.", "unlocked": False},
    "hundred_flesh": {"name": "Flesh Pile", "desc": "Reach 100 flesh.", "unlocked": False},
    "thousand_flesh": {"name": "Flesh Mountain", "desc": "Reach 1000 flesh.", "unlocked": False},
}

# Base upgrades: id -> data
# type: "click" (adds flesh per click), "auto" (adds flesh per second), etc.
BASE_UPGRADES = {
    "bigger_clicks": {
        "name": "Bigger Clicks",
        "desc": "+1 flesh per click.",
        "type": "click",
        "category": "click",
        "base_cost": 10,
        "cost_mult": 1.15,
        "fps": 0.0,
        "fpc": 1.0
    },
    "auto_clicker_1": {
        "name": "Autoclicker Mk.I",
        "desc": "+1 flesh/sec per unit.",
        "type": "auto",
        "category": "auto",
        "base_cost": 25,
        "cost_mult": 1.15,
        "fps": 1.0,
        "fpc": 0.0
    },
    "auto_clicker_2": {
        "name": "Autoclicker Mk.II",
        "desc": "+2 flesh/sec per unit.",
        "type": "auto",
        "category": "auto",
        "base_cost": 100,
        "cost_mult": 1.18,
        "fps": 2.0,
        "fpc": 0.0
    },
    "crit_click": {
        "name": "Critical Clicks",
        "desc": "Chance for double flesh per click.",
        "type": "click",
        "category": "click",
        "base_cost": 200,
        "cost_mult": 1.2,
        "fps": 0.0,
        "fpc": 0.0
    },
}


def load_json(path, default, legacy_path=None):
    """Load JSON from *path*; optionally fall back to *legacy_path*.

    This lets us migrate old saves that used to live next to the script
    into the new XDG config directory without breaking older installs.
    """
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
            # Try to migrate into the new location
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
    """Read the flat text counter used by earlier versions.

    We first prefer the new COUNTER_FILE in CONFIG_DIR (current saves),
    then fall back to the legacy file next to the script.
    """
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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_LEADERBOARD_TABLE = os.environ.get("SUPABASE_LEADERBOARD_TABLE", "leaderboard")


def get_default_username() -> str:
    """Best-effort OS username detection.

    Tries os.getlogin(); if that fails (Wayland / flatpak sandbox),
    falls back to the leaf folder name of $HOME. """
    try:
        return os.getlogin()
    except Exception:
        home = os.path.expanduser("~")
        name = os.path.basename(home.rstrip(os.sep))
        return name or "unknown"


def _leaderboard_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def submit_leaderboard_entry(username: str, flesh_amount: int):
    """Send a single row to the Supabase leaderboard table.

    Returns (ok: bool, info: str | dict). """
    if not _leaderboard_configured():
        return False, "SUPABASE_URL / SUPABASE_KEY not configured in environment"

    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SUPABASE_LEADERBOARD_TABLE}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    payload = {
        "username": username,
        "flesh_amount": int(flesh_amount),
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        return False, f"Request error: {e}"

    if resp.status_code not in (200, 201):
        # include a short prefix of the body for debugging
        body = resp.text[:200]
        return False, f"HTTP {resp.status_code}: {body}"

    try:
        data = resp.json()
    except Exception:
        data = {}
    return True, data


def fetch_leaderboard_entries():
    """Fetch all leaderboard rows ordered by flesh_amount descending.

    Returns (ok: bool, error: str, rows: list[dict]). """
    if not _leaderboard_configured():
        return False, "SUPABASE_URL / SUPABASE_KEY not configured in environment", []

    # order by flesh_amount desc; Supabase uses this query syntax.
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
        body = resp.text[:200]
        return False, f"HTTP {resp.status_code}: {body}", []

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

        # data
        self.settings     = load_json(SETTINGS_FILE,     DEFAULT_SETTINGS,     legacy_path=LEGACY_SETTINGS_FILE)
        self.state        = load_json(STATE_FILE,        DEFAULT_STATE,        legacy_path=LEGACY_STATE_FILE)
        self.achievements = load_json(ACHIEVEMENTS_FILE, DEFAULT_ACHIEVEMENTS, legacy_path=LEGACY_ACHIEVEMENTS_FILE)

        # ensure keys exist
        for k, v in DEFAULT_SETTINGS.items():
            self.settings.setdefault(k, v)
        for k, v in DEFAULT_STATE.items():
            if k not in self.state:
                self.state[k] = v
        if "upgrades_owned" not in self.state:
            self.state["upgrades_owned"] = {}
        for k, v in DEFAULT_ACHIEVEMENTS.items():
            if k not in self.achievements:
                self.achievements[k] = v

        # if starting from very old version, sync flesh with legacy counter
        if self.state["flesh"] == 0:
            legacy = load_legacy_counter()
            if legacy > 0:
                self.state["flesh"] = legacy

        self.upgrades = dict(BASE_UPGRADES)  # mods can add more

        # track time for RPC
        self.start_time = int(time.time())
        self.rpc_last_update = 0
        self.rpc = None

        # CSS / style
        css = """
        window {
            background-color: #151515;
        }
        .flesh-picture {
            border-radius: 12px;
        }
        .flesh-picture.squish {
            transform: scale(0.94);
            transition: transform 80ms ease-out;
        }
        .upgrade-row {
            padding: 6px;
        }
        .achievement-row {
            padding: 4px;
        }
        .badge-unlocked {
            color: #a6e3a1;
        }
        .badge-locked {
            color: #f38ba8;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode("utf-8"))
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # load mods
        self.load_mods()

        # init UI
        self.build_ui()
        self.update_labels()

        # auto-timer
        GLib.timeout_add(1000, self.on_timer_tick)

        # RPC
        if RPC_AVAILABLE and self.settings.get("enable_rpc") and self.settings.get("discord_client_id"):
            self.init_rpc()
            GLib.timeout_add(2000, self.tick_rpc_update)

    # ---------- DATA / STATS ----------

    @property
    def flesh(self) -> float:
        return self.state["flesh"]

    @flesh.setter
    def flesh(self, value: float):
        self.state["flesh"] = max(0.0, float(value))
        save_json(STATE_FILE, self.state)
        save_legacy_counter(self.state["flesh"])

    def add_flesh(self, amount: float):
        self.flesh = self.flesh + amount

    def get_upgrade_count(self, uid: str) -> int:
        return int(self.state["upgrades_owned"].get(uid, 0))

    def set_upgrade_count(self, uid: str, value: int):
        self.state["upgrades_owned"][uid] = int(value)
        save_json(STATE_FILE, self.state)

    def total_upgrades_owned(self) -> int:
        return sum(self.state["upgrades_owned"].values())

    def compute_fps(self) -> float:
        fps = 0.0
        for uid, u in self.upgrades.items():
            if u.get("type") == "auto":
                count = self.get_upgrade_count(uid)
                fps += u.get("fps", 0.0) * count
        return fps

    def compute_extra_fpc(self) -> float:
        extra = 0.0
        for uid, u in self.upgrades.items():
            if u.get("type") == "click":
                count = self.get_upgrade_count(uid)
                extra += u.get("fpc", 0.0) * count
        return extra

    def effective_fpc(self) -> float:
        base = self.state.get("flesh_per_click", 1.0)
        return base + self.compute_extra_fpc()

    def on_filter_clicked(self, button, category_key):
        """Switch upgrade filter and rebuild list."""
        self.current_filter = category_key
        self.refresh_upgrades_ui()



    # ---------- DISCORD RPC ----------

    def init_rpc(self):
        try:
            self.rpc = Presence(self.settings["discord_client_id"])
            self.rpc.connect()
        except Exception:
            self.rpc = None

    def tick_rpc_update(self):
        if not self.rpc:
            return True  # keep timer
        now = time.time()
        if now - self.rpc_last_update < 10:
            return True
        self.rpc_last_update = now
        try:
            self.rpc.update(
                state=f"{int(self.flesh)} flesh",
                details=f"{self.compute_fps():.1f} flesh/sec",
                large_image="flesh",
                large_text="Flesh Clicker",
                start=self.start_time,
            )
        except Exception:
            pass
        return True

    # ---------- MOD LOADING ----------

    def load_mods(self):
        """Load mods from both system-wide and per-user directories."""
        for mods_dir in (SYSTEM_MODS_DIR, USER_MODS_DIR):
            if not os.path.isdir(mods_dir):
                continue
            for fname in os.listdir(mods_dir):
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(mods_dir, fname)
                try:
                    loader = importlib.machinery.SourceFileLoader(fname, path)
                    spec = importlib.util.spec_from_loader(loader.name, loader)
                    mod = importlib.util.module_from_spec(spec)
                    loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(self)
                except Exception:
                    # silently ignore broken mods
                    continue
        # after mods, refresh UI
        self.refresh_upgrades_ui()
        self.refresh_achievements_ui()

    # helper for mods
    def register_upgrade(self, uid: str, data: dict):
        if uid in self.upgrades:
            self.upgrades[uid].update(data)
        else:
            self.upgrades[uid] = data

    def register_achievement(self, key: str, data: dict):
        if key in self.achievements:
            self.achievements[key].update(data)
        else:
            self.achievements[key] = data

    # ---------- UI BUILD ----------

    def build_ui(self):
        root = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        root.set_wide_handle(True)
        self.set_child(root)

        # LEFT: main click area
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_margin_top(12)
        left_box.set_margin_bottom(12)
        left_box.set_margin_start(12)
        left_box.set_margin_end(6)

        # picture placeholder
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

        # labels
        self.flesh_label = Gtk.Label(xalign=0)
        self.fps_label   = Gtk.Label(xalign=0)

        stats_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        stats_box.append(self.flesh_label)
        stats_box.append(self.fps_label)
        left_box.append(stats_box)

        root.set_start_child(left_box)

        # RIGHT: notebook with tabs
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_box.set_margin_top(12)
        right_box.set_margin_bottom(12)
        right_box.set_margin_start(6)
        right_box.set_margin_end(12)

        self.notebook = Gtk.Notebook()
        right_box.append(self.notebook)

        # Upgrades page
        self.upgrades_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_upgrades_page()
        self.notebook.append_page(self.upgrades_page, Gtk.Label(label="Upgrades"))

        # Achievements page
        self.achievements_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_achievements_page()
        self.notebook.append_page(self.achievements_page, Gtk.Label(label="Achievements"))

        # Leaderboard page
        self.leaderboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_leaderboard_page()
        self.notebook.append_page(self.leaderboard_page, Gtk.Label(label="Leaderboard"))

        # Settings page
        self.settings_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_settings_page()
        self.notebook.append_page(self.settings_page, Gtk.Label(label="Settings"))

        self.refresh_upgrades_ui()
        self.refresh_achievements_ui()
        root.set_end_child(right_box)

    def build_upgrades_page(self):
        self.upgrades_page.set_margin_top(4)
        self.upgrades_page.set_margin_bottom(4)
        self.upgrades_page.set_margin_start(4)
        self.upgrades_page.set_margin_end(4)

        # filter buttons
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.upgrade_filter_buttons = {}

        def make_filter_button(label, key):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self.on_filter_clicked, key)
            self.upgrade_filter_buttons[key] = btn
            filter_box.append(btn)

        make_filter_button("All", "all")
        make_filter_button("Click", "click")
        make_filter_button("Auto", "auto")

        self.upgrades_page.append(filter_box)

        self.upgrades_listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.current_filter = "all"
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.upgrades_listbox)
        scroll.set_vexpand(True)
        self.upgrades_page.append(scroll)

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
        self.settings_page.append(grid)

        row = 0

        # RPC toggle
        rpc_label = Gtk.Label(label="Enable Discord RPC", xalign=0)
        grid.attach(rpc_label, 0, row, 1, 1)
        self.rpc_switch = Gtk.Switch()
        self.rpc_switch.set_active(bool(self.settings.get("enable_rpc")))
        self.rpc_switch.connect("notify::active", self.on_settings_changed)
        grid.attach(self.rpc_switch, 1, row, 1, 1)
        row += 1

        # RPC client ID
        id_label = Gtk.Label(label="Discord Client ID", xalign=0)
        grid.attach(id_label, 0, row, 1, 1)
        self.rpc_entry = Gtk.Entry()
        self.rpc_entry.set_text(self.settings.get("discord_client_id", ""))
        self.rpc_entry.connect("changed", self.on_settings_changed)
        grid.attach(self.rpc_entry, 1, row, 1, 1)
        row += 1

        # Squish ms
        squish_label = Gtk.Label(label="Squish duration (ms)", xalign=0)
        grid.attach(squish_label, 0, row, 1, 1)
        self.squish_spin = Gtk.SpinButton.new_with_range(20, 300, 10)
        self.squish_spin.set_value(int(self.settings.get("squish_ms", 100)))
        self.squish_spin.connect("value-changed", self.on_settings_changed)
        grid.attach(self.squish_spin, 1, row, 1, 1)
        row += 1

        # Click sound (future use)
        sound_label = Gtk.Label(label="Play click sound", xalign=0)
        grid.attach(sound_label, 0, row, 1, 1)
        self.sound_switch = Gtk.Switch()
        self.sound_switch.set_active(bool(self.settings.get("play_click_sound")))
        self.sound_switch.connect("notify::active", self.on_settings_changed)
        grid.attach(self.sound_switch, 1, row, 1, 1)

        row += 1

        # Leaderboard: manual submit button
        leaderboard_button = Gtk.Button(label="Add leaderboard entry")
        leaderboard_button.connect("clicked", self.on_add_leaderboard_clicked)
        grid.attach(leaderboard_button, 0, row, 2, 1)
        row += 1

        # status label for settings actions (e.g. leaderboard submit)
        self.settings_info_label = Gtk.Label(label="", xalign=0)
        self.settings_page.append(self.settings_info_label)

    # ---------- LEADERBOARD UI ----------

    def build_leaderboard_page(self):
        self.leaderboard_page.set_margin_top(4)
        self.leaderboard_page.set_margin_bottom(4)
        self.leaderboard_page.set_margin_start(4)
        self.leaderboard_page.set_margin_end(4)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.leaderboard_page.append(outer)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.append(controls)

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
            # older gi bindings might not have set_monospace; ignore
            pass
        scrolled.set_child(self.leaderboard_textview)

        # initial load
        self.load_leaderboard()

    def update_leaderboard_view(self, rows):
        """Render rows (list of dicts) into the text view as a simple table."""
        buffer = self.leaderboard_textview.get_buffer()
        if not rows:
            buffer.set_text("No leaderboard entries yet.")
            return

        header = f"{'Username':<20} {'Flesh':>10}  {'Timestamp':<32}\n"
        line = "-" * 64 + "\n"
        out_lines = [header, line]
        for row in rows:
            username = str(row.get("username") or "?")
            flesh = row.get("flesh_amount")
            if flesh is None:
                flesh = row.get("flesh")
            try:
                flesh_str = str(int(flesh))
            except Exception:
                flesh_str = str(flesh)
            ts = row.get("last_update") or row.get("created_at") or ""
            out_lines.append(f"{username:<20} {flesh_str:>10}  {ts:<32}\n")

        buffer.set_text("".join(out_lines))

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
        """Settings → "Add leaderboard entry" button handler."""
        username = get_default_username()
        flesh_amount = int(self.flesh)

        ok, info = submit_leaderboard_entry(username, flesh_amount)
        if ok:
            msg = f"Submitted leaderboard entry as '{username}' with {flesh_amount} flesh."
            # try refreshing the leaderboard tab too
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
        """Load the main flesh image (always flesh.png)."""
        path = os.path.join(BASE_DIR, "flesh.png")

        try:
            texture = Gdk.Texture.new_from_filename(path)
            self.picture.set_paintable(texture)
            return
        except Exception as e:
            print("Failed to load flesh.png:", e)
            # Do nothing → picture stays empty

    # ---------- UI HELPERS ----------

    def clear_box_children(self, box: Gtk.Box):
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def refresh_upgrades_ui(self):
        if not hasattr(self, "upgrades_listbox"):
            return
        self.clear_box_children(self.upgrades_listbox)

        for uid, u in self.upgrades.items():
            cat = u.get("category", "misc")

            # simple filter; could store current filter on self
            # for now, always show all
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.add_css_class("upgrade-row")

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            name_label = Gtk.Label(label=f"{u['name']} ({cat})", xalign=0)
            name_label.set_hexpand(True)
            name_label.set_xalign(0.0)
            top.append(name_label)

            cost_label = Gtk.Label(
                label=f"Cost: {int(self.get_upgrade_cost(uid, self.get_upgrade_count(uid)))} flesh",
                xalign=1
            )
            top.append(cost_label)

            row.append(top)

            desc_label = Gtk.Label(label=u["desc"], xalign=0)
            desc_label.set_wrap(True)
            row.append(desc_label)

            btn = Gtk.Button(label="Buy")
            btn.connect("clicked", self.on_buy_upgrade_clicked, uid)
            row.append(btn)

            self.upgrades_listbox.append(row)
    def refresh_achievements_ui(self):
        if not hasattr(self, "achievements_listbox"):
            return

        self.clear_box_children(self.achievements_listbox)

        for key, data in self.achievements.items():
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.add_css_class("achievement-row")

            unlocked = data.get("unlocked", False)
            name = data.get("name", key)
            desc = data.get("desc", "")

            name_label = Gtk.Label(label=name, xalign=0)
            desc_label = Gtk.Label(label=desc, xalign=0)
            desc_label.set_wrap(True)

            if unlocked:
                name_label.add_css_class("badge-unlocked")
            else:
                name_label.add_css_class("badge-locked")

            row.append(name_label)
            row.append(desc_label)
            self.achievements_listbox.append(row)



    # ---------- TIMER / GAME LOOP ----------

    def on_timer_tick(self):
        # auto-flesh from upgrades
        fps = self.compute_fps()
        if fps > 0:
            self.add_flesh(fps)
        self.update_labels()
        return True  # keep timer

    def update_labels(self):
        self.flesh_label.set_text(f"Flesh: {int(self.flesh)}")
        self.fps_label.set_text(f"Flesh per second: {self.compute_fps():.1f}")

    # ---------- UPGRADE LOGIC ----------

    def get_upgrade_cost(self, uid: str, owned: int) -> float:
        u = self.upgrades[uid]
        base = u["base_cost"]
        mult = u["cost_mult"]
        return base * (mult ** owned)

    def on_buy_upgrade_clicked(self, button, uid: str):
        owned = self.get_upgrade_count(uid)
        cost = self.get_upgrade_cost(uid, owned)
        if self.flesh < cost:
            # not enough flesh
            return
        self.add_flesh(-cost)
        self.set_upgrade_count(uid, owned + 1)

        # track first_upgrade / five_upgrades achievements
        total = self.total_upgrades_owned()
        if total >= 1:
            self.unlock_achievement("first_upgrade")
        if total >= 5:
            self.unlock_achievement("five_upgrades")

        self.refresh_upgrades_ui()
        self.update_labels()

    # ---------- CLICK HANDLER ----------

    def on_click(self, gesture, n_press, x, y):
        # flesh gain logic
        fpc = self.effective_fpc()

        # simple crit from crit_click upgrade
        crit_chance = 0.0
        crit_upgrade_count = self.get_upgrade_count("crit_click")
        if crit_upgrade_count > 0:
            crit_chance = 0.05 * crit_upgrade_count  # 5% per level, just an example
        if random.random() < crit_chance:
            fpc *= 2.0

        self.add_flesh(fpc)
        self.state["total_clicks"] += 1
        save_json(STATE_FILE, self.state)

        # achievements
        if self.state["total_clicks"] >= 1:
            self.unlock_achievement("first_click")
        if self.state["total_clicks"] >= 10:
            self.unlock_achievement("ten_clicks")
        if self.state["total_clicks"] >= 100:
            self.unlock_achievement("hundred_clicks")

        if self.flesh >= 100:
            self.unlock_achievement("hundred_flesh")
        if self.flesh >= 1000:
            self.unlock_achievement("thousand_flesh")

        self.update_labels()

    # ---------- ACHIEVEMENTS ----------

    def unlock_achievement(self, key: str):
        if key not in self.achievements:
            return
        if not self.achievements[key].get("unlocked", False):
            self.achievements[key]["unlocked"] = True
            save_json(ACHIEVEMENTS_FILE, self.achievements)

    # ---------- SETTINGS ----------

    def on_settings_changed(self, *args):
        self.settings["enable_rpc"] = self.rpc_switch.get_active()
        self.settings["discord_client_id"] = self.rpc_entry.get_text().strip()
        self.settings["squish_ms"] = int(self.squish_spin.get_value())
        self.settings["play_click_sound"] = self.sound_switch.get_active()
        save_json(SETTINGS_FILE, self.settings)


class FleshApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None

    def do_activate(self, *args):
        if not self.window:
            self.window = FleshClicker(self)
        self.window.present()


def main():
    app = FleshApp()
    app.run()


if __name__ == "__main__":
    main()
