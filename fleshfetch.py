#!/usr/bin/env python3
import os
os.environ["GDK_SCALE"] = "1"
os.environ["GDK_DPI_SCALE"] = "1"
os.environ["GTK_THEME"] = "Adwaita"
import sys
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

# ---------- PATHS ----------
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.name == "nt":
    CONFIG_DIR = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "fleshfetch"
    )
else:
    _xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    CONFIG_DIR = os.path.join(_xdg, "fleshfetch")

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
    "discord_client_id": "",
    "squish_ms": 100,
    "play_click_sound": False,
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
        self.settings     = load_json(SETTINGS_FILE,     DEFAULT_SETTINGS,     legacy_path=LEGACY_SETTINGS_FILE)
        self.state        = load_json(STATE_FILE,        DEFAULT_STATE,        legacy_path=LEGACY_STATE_FILE)
        self.achievements = load_json(ACHIEVEMENTS_FILE, DEFAULT_ACHIEVEMENTS, legacy_path=LEGACY_ACHIEVEMENTS_FILE)

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

        for k, v in DEFAULT_ACHIEVEMENTS.items():
            if k not in self.achievements:
                self.achievements[k] = v

        # sync flesh with legacy counter if starting fresh
        if self.state["currencies"].get("flesh", 0.0) == 0:
            legacy = load_legacy_counter()
            if legacy > 0:
                self.state["currencies"]["flesh"] = float(legacy)

        # ---------- registries ----------
        self.currencies = dict(BASE_CURRENCIES)
        self.upgrades   = dict(BASE_UPGRADES)

        # primary currency: used for base per-click gain and legacy counter
        self.primary_currency = "flesh"

        # mods can override this to change the clickable image
        self.flesh_image_path = os.path.join(BASE_DIR, "flesh.png")

        self.start_time      = int(time.time())
        self.rpc_last_update = 0
        self.rpc             = None

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

        if RPC_AVAILABLE and self.settings.get("enable_rpc") and self.settings.get("discord_client_id"):
            self.init_rpc()
            GLib.timeout_add(2000, self.tick_rpc_update)

    # ---------- CURRENCY HELPERS ----------

    def get_currency(self, registry_name: str) -> float:
        return float(self.state["currencies"].get(registry_name, 0.0))

    def set_currency(self, registry_name: str, value: float):
        self.state["currencies"][registry_name] = max(0.0, float(value))
        save_json(STATE_FILE, self.state)
        if registry_name == self.primary_currency:
            save_legacy_counter(self.state["currencies"][registry_name])

    def add_currency(self, registry_name: str, amount: float):
        self.set_currency(registry_name, self.get_currency(registry_name) + amount)

    # legacy property so leaderboard code keeps working
    @property
    def flesh(self) -> float:
        return self.get_currency(self.primary_currency)

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
            return True
        now = time.time()
        if now - self.rpc_last_update < 10:
            return True
        self.rpc_last_update = now
        try:
            self.rpc.update(
                state=f"{int(self.flesh)} flesh",
                details=f"{self.compute_cps('flesh'):.1f} flesh/sec",
                large_image="flesh",
                large_text="Flesh Clicker",
                start=self.start_time,
            )
        except Exception:
            pass
        return True

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

                try:
                    loader = importlib.machinery.SourceFileLoader(entry, mod_py)
                    spec   = importlib.util.spec_from_loader(loader.name, loader)
                    mod    = importlib.util.module_from_spec(spec)
                    mod.MOD_DIR  = mod_dir
                    mod.MANIFEST = manifest
                    loader.exec_module(mod)
                    if hasattr(mod, "register"):
                        mod.register(self)
                except Exception as e:
                    print(f"[mods] Failed to load '{entry}': {e}")

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
        if key in self.achievements:
            self.achievements[key].update(data)
        else:
            self.achievements[key] = data

    def set_flesh_image(self, path: str):
        """Override the clickable image. Call from register() before build_ui runs."""
        self.flesh_image_path = path

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
        """Remove all built-in upgrades, achievements, and the flesh currency,
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

        # remove flesh from the currency registry and saved state
        self.currencies.pop("flesh", None)
        self.state["currencies"].pop("flesh", None)

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

        self.achievements_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_achievements_page()
        self.notebook.append_page(self.achievements_page, Gtk.Label(label="Achievements"))

        self.leaderboard_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.build_leaderboard_page()
        self.notebook.append_page(self.leaderboard_page, Gtk.Label(label="Leaderboard"))

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

        rpc_label = Gtk.Label(label="Enable Discord RPC", xalign=0)
        grid.attach(rpc_label, 0, row, 1, 1)
        self.rpc_switch = Gtk.Switch()
        self.rpc_switch.set_active(bool(self.settings.get("enable_rpc")))
        self.rpc_switch.connect("notify::active", self.on_settings_changed)
        grid.attach(self.rpc_switch, 1, row, 1, 1)
        row += 1

        id_label = Gtk.Label(label="Discord Client ID", xalign=0)
        grid.attach(id_label, 0, row, 1, 1)
        self.rpc_entry = Gtk.Entry()
        self.rpc_entry.set_text(self.settings.get("discord_client_id", ""))
        self.rpc_entry.connect("changed", self.on_settings_changed)
        grid.attach(self.rpc_entry, 1, row, 1, 1)
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
        self.sound_switch.set_active(bool(self.settings.get("play_click_sound")))
        self.sound_switch.connect("notify::active", self.on_settings_changed)
        grid.attach(self.sound_switch, 1, row, 1, 1)
        row += 1

        leaderboard_button = Gtk.Button(label="Add leaderboard entry")
        leaderboard_button.connect("clicked", self.on_add_leaderboard_clicked)
        grid.attach(leaderboard_button, 0, row, 2, 1)
        row += 1

        self.settings_info_label = Gtk.Label(label="", xalign=0)
        self.settings_page.append(self.settings_info_label)

    # ---------- LEADERBOARD UI ----------

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
            buffer.set_text("No leaderboard entries yet.")
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
        buffer.set_text("".join(lines))

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
        try:
            texture = Gdk.Texture.new_from_filename(self.flesh_image_path)
            self.picture.set_paintable(texture)
        except Exception as e:
            print(f"Failed to load image '{self.flesh_image_path}':", e)

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

    def refresh_upgrades_ui(self):
        if not hasattr(self, "upgrades_listbox"):
            return
        self.clear_box_children(self.upgrades_listbox)

        selected_filter = getattr(self, "current_filter", "all")
        if hasattr(self, "upgrade_filter_buttons"):
            for key, btn in self.upgrade_filter_buttons.items():
                if key == selected_filter:
                    btn.add_css_class("suggested-action")
                else:
                    btn.remove_css_class("suggested-action")

        for uid, u in self.upgrades.items():
            cat = u.get("category") or u.get("type") or "misc"
            if selected_filter != "all" and cat != selected_filter:
                continue

            # hide upgrades whose requirements are not yet met
            if not self.check_requires(uid):
                continue

            owned         = self.get_upgrade_count(uid)
            cost          = self.get_upgrade_cost(uid, owned)
            cost_currency = u.get("cost_currency", "flesh")
            cost_display  = self.currencies.get(cost_currency, {}).get("display_name", cost_currency)

            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.add_css_class("upgrade-row")

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            name_label = Gtk.Label(label=f"{u['name']} ({cat})", xalign=0)
            name_label.set_hexpand(True)
            name_label.set_xalign(0.0)
            top.append(name_label)

            cost_label = Gtk.Label(label=f"Cost: {int(cost)} {cost_display}", xalign=1)
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
            row      = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            unlocked = data.get("unlocked", False)
            row.add_css_class("achievement-row")
            name_label = Gtk.Label(label=data.get("name", key), xalign=0)
            desc_label = Gtk.Label(label=data.get("desc", ""),  xalign=0)
            desc_label.set_wrap(True)
            name_label.add_css_class("badge-unlocked" if unlocked else "badge-locked")
            row.append(name_label)
            row.append(desc_label)
            self.achievements_listbox.append(row)

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

        self.refresh_upgrades_ui()
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
        if not self.achievements[key].get("unlocked", False):
            self.achievements[key]["unlocked"] = True
            save_json(ACHIEVEMENTS_FILE, self.achievements)

    # ---------- SETTINGS ----------

    def on_settings_changed(self, *args):
        self.settings["enable_rpc"]        = self.rpc_switch.get_active()
        self.settings["discord_client_id"] = self.rpc_entry.get_text().strip()
        self.settings["squish_ms"]         = int(self.squish_spin.get_value())
        self.settings["play_click_sound"]  = self.sound_switch.get_active()
        save_json(SETTINGS_FILE, self.settings)


class FleshApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
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
