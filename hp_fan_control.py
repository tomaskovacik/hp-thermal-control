#!/usr/bin/env python3
"""HP ZBook Firefly 14 G10 — Thermal Control System Tray Applet

Tray icon shows live CPU temp + fan RPM.
Click "Open Thermal Monitor" to show full sensor dashboard with controls:
  - Turbo Boost toggle (runtime via intel_pstate)
  - RAPL PL1 power cap slider with optional boot persistence (tmpfiles.d)
  - Turbo Boost persistent toggle: via BIOS password or GRUB kernel parameter
  - Fan Always On while on AC (BIOS setting, requires BIOS password)

Privileged writes go through pkexec + /usr/local/sbin/hp-thermal-helper (polkit).
BIOS admin password stored in GNOME Keyring (Seahorse).
"""

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AppIndicator3', '0.1')
gi.require_version('Secret', '1')

from gi.repository import Gtk, GLib, AppIndicator3, Secret
import os
import subprocess
import threading
import logging
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(os.path.expanduser("~/hp-thermal-applet.log")),
    ]
)
log = logging.getLogger("hp-thermal")

# ── Paths ─────────────────────────────────────────────────────────────────────
HELPER        = "/usr/local/sbin/hp-thermal-helper"
NO_TURBO      = "/sys/devices/system/cpu/intel_pstate/no_turbo"
RAPL_PL1      = "/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"
BIOSCFG_ATTRS = "/sys/class/firmware-attributes/hp-bioscfg/attributes"

BIOS_TURBO_ATTR  = "Turbo-boost"
BIOS_FAN_AC_ATTR = "Fan Always on while on AC Power"

# ── Keyring ───────────────────────────────────────────────────────────────────
_SECRET_SCHEMA = Secret.Schema.new(
    "com.hp.thermal",
    Secret.SchemaFlags.NONE,
    {"service": Secret.SchemaAttributeType.STRING},
)
_SECRET_ATTRS = {"service": "bios-admin"}

def keyring_get():
    return Secret.password_lookup_sync(_SECRET_SCHEMA, _SECRET_ATTRS, None)

def keyring_store(password):
    Secret.password_store_sync(
        _SECRET_SCHEMA, _SECRET_ATTRS,
        Secret.COLLECTION_DEFAULT,
        "HP BIOS Administrator Password",
        password, None,
    )

# ── Privileged helper ─────────────────────────────────────────────────────────
def run_helper(*args, stdin_data=None):
    """Call pkexec hp-thermal-helper <args>, optionally with stdin_data (bytes)."""
    try:
        result = subprocess.run(
            ["pkexec", HELPER] + list(args),
            input=stdin_data,
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0, result.stderr.decode().strip()
    except Exception as e:
        return False, str(e)

def helper_turbo(enabled: bool):
    run_helper("turbo", "0" if enabled else "1")

def helper_rapl(watts: int):
    run_helper("rapl", str(watts * 1_000_000))

def helper_bios_set(attr_name: str, value: str, password: str):
    stdin = f"{password}\n{value}\n".encode()
    ok, err = run_helper("bios-set", attr_name, stdin_data=stdin)
    return ok, err

# ── Sensor helpers ────────────────────────────────────────────────────────────
def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default

def _read_int(path, default=0):
    try:
        return int(_read(path, str(default)))
    except Exception:
        return default

def find_hwmon(name):
    base = "/sys/class/hwmon"
    for d in sorted(os.listdir(base)):
        if _read(f"{base}/{d}/name") == name:
            return f"{base}/{d}"
    return None

def build_core_type_map():
    """Map coretemp core_id → ('P'/'E', display_index).

    P-cores have Hyper-Threading (2 logical CPUs per physical core).
    E-cores have no HT (1 logical CPU per physical core).
    Returns empty dict if topology info is unavailable.
    """
    cpu_base = "/sys/devices/system/cpu"
    core_siblings = {}
    try:
        for entry in os.listdir(cpu_base):
            if not entry.startswith("cpu") or not entry[3:].isdigit():
                continue
            core_id_str = _read(f"{cpu_base}/{entry}/topology/core_id", "")
            if not core_id_str:
                continue
            core_id = int(core_id_str)
            core_siblings.setdefault(core_id, 0)
            core_siblings[core_id] += 1
    except Exception:
        return {}

    result = {}
    p_idx = e_idx = 0
    for core_id in sorted(core_siblings):
        if core_siblings[core_id] > 1:
            result[core_id] = ("P", p_idx); p_idx += 1
        else:
            result[core_id] = ("E", e_idx); e_idx += 1
    return result

_CORE_TYPE_MAP = None

def _core_label(coretemp_label):
    """Translate 'Core N' → 'P-Core X' or 'E-Core X' for hybrid CPUs."""
    global _CORE_TYPE_MAP
    if _CORE_TYPE_MAP is None:
        _CORE_TYPE_MAP = build_core_type_map()
    if not _CORE_TYPE_MAP or not coretemp_label.startswith("Core "):
        return coretemp_label
    try:
        core_id = int(coretemp_label.split()[1])
    except (IndexError, ValueError):
        return coretemp_label
    info = _CORE_TYPE_MAP.get(core_id)
    if not info:
        return coretemp_label
    kind, idx = info
    return f"{kind}-Core {idx}"

def gather_sensors():
    """Return dict of all available sensor readings."""
    data = {}

    # hp_wmi_sensors — labeled temperatures + fan RPM
    hp = find_hwmon("hp_wmi_sensors")
    if hp:
        for i in range(1, 8):
            val  = _read_int(f"{hp}/temp{i}_input")
            lbl  = _read(f"{hp}/temp{i}_label", "")
            if lbl and val > 0:
                data[lbl] = ("temp", val // 1000)
        rpm1 = _read_int(f"{hp}/fan1_input")
        rpm2 = _read_int(f"{hp}/fan2_input")
        data["CPU Fan"] = ("fan", rpm1)
        if rpm2 > 0:
            data["GPU Fan"] = ("fan", rpm2)

    # coretemp — package + per-core with P/E-core labels
    ct = find_hwmon("coretemp")
    if ct:
        pkg = _read_int(f"{ct}/temp1_input")
        if pkg > 0:
            data["CPU Package"] = ("temp", pkg // 1000)
        for i in range(2, 20):
            lbl = _read(f"{ct}/temp{i}_label", "")
            val = _read_int(f"{ct}/temp{i}_input")
            if lbl and val > 0:
                data[_core_label(lbl)] = ("temp", val // 1000)

    # NVMe
    nv = find_hwmon("nvme")
    if nv:
        v = _read_int(f"{nv}/temp1_input")
        if v > 0:
            data["NVMe"] = ("temp", v // 1000)

    # WiFi
    wf = find_hwmon("iwlwifi_1")
    if wf:
        v = _read_int(f"{wf}/temp1_input")
        if v > 0:
            data["WiFi"] = ("temp", v // 1000)

    # Controls state
    data["_turbo_on"]   = _read_int(NO_TURBO, 1) == 0
    data["_rapl_w"]     = _read_int(RAPL_PL1, 0) // 1_000_000
    data["_bios_turbo"] = _read(f"{BIOSCFG_ATTRS}/{BIOS_TURBO_ATTR}/current_value", "Unknown")
    data["_bios_fan"]   = _read(f"{BIOSCFG_ATTRS}/{BIOS_FAN_AC_ATTR}/current_value", "Unknown")

    # Persistence state
    tmpfiles = "/etc/tmpfiles.d/hp-thermal.conf"
    data["_rapl_persistent"] = False
    if os.path.exists(tmpfiles):
        try:
            content = open(tmpfiles).read()
            data["_rapl_persistent"] = "constraint_0_power_limit_uw" in content
        except Exception:
            pass

    grub_def = _read("/etc/default/grub", "")
    if "intel_pstate=no_turbo" in grub_def:
        data["_turbo_persist_method"] = "GRUB"
    else:
        data["_turbo_persist_method"] = "BIOS"

    return data

# ── Password dialog ───────────────────────────────────────────────────────────
class BiosPasswordDialog(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title="BIOS Administrator Password", transient_for=parent, modal=bool(parent))
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                         Gtk.STOCK_OK,     Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(380, -1)
        self.set_keep_above(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        # Allow closing with the X button
        self.connect("delete-event", lambda *_: self.response(Gtk.ResponseType.CANCEL))

        area = self.get_content_area()
        area.set_spacing(10)
        area.set_margin_start(16); area.set_margin_end(16)
        area.set_margin_top(16);   area.set_margin_bottom(16)

        lbl = Gtk.Label(label="Enter the HP BIOS Administrator password\nto change BIOS settings:")
        lbl.set_xalign(0)
        area.add(lbl)

        self._entry = Gtk.Entry()
        self._entry.set_visibility(False)
        self._entry.set_activates_default(True)
        area.add(self._entry)

        self._remember = Gtk.CheckButton(label="Remember in GNOME Keyring (Seahorse)")
        self._remember.set_active(True)
        area.add(self._remember)

        self.show_all()

    @property
    def password(self):
        return self._entry.get_text()

    @property
    def remember(self):
        return self._remember.get_active()

# ── Main applet ───────────────────────────────────────────────────────────────
class ThermalApplet:
    # Sensor display order (others appended alphabetically)
    _SENSOR_ORDER = [
        "CPU Temperature", "CPU Package",
        "P-Core 0", "P-Core 1", "P-Core 2", "P-Core 3",
        "E-Core 0", "E-Core 1", "E-Core 2", "E-Core 3",
        "E-Core 4", "E-Core 5", "E-Core 6", "E-Core 7",
        "Discrete Graphics Temperature",
        "Remote Temperature", "Local Temperature", "Battery Temperature",
        "NVMe", "WiFi", "CPU Fan", "GPU Fan",
    ]

    def __init__(self):
        self._bios_password = keyring_get()
        self._updating_ui   = False
        self._rapl_timer    = None
        self._window        = None
        self._turbo_persist = None  # 'bios' or 'grub' or None

        # ── AppIndicator ──────────────────────────────────────────────────────
        self._ind = AppIndicator3.Indicator.new(
            "hp-thermal-applet",
            "temperature-symbolic",
            AppIndicator3.IndicatorCategory.HARDWARE,
        )
        self._ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._ind.set_label("--°C", "000°C 0000rpm")

        menu = Gtk.Menu()
        item_open = Gtk.MenuItem(label="Open Thermal Monitor")
        item_open.connect("activate", lambda *_: self._show_window())
        menu.append(item_open)
        menu.append(Gtk.SeparatorMenuItem())
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(item_quit)
        menu.show_all()
        self._ind.set_menu(menu)

        # ── Build window ──────────────────────────────────────────────────────
        self._build_window()

        # ── Start polling ─────────────────────────────────────────────────────
        GLib.timeout_add_seconds(3, self._poll)
        self._poll()

    # ── Window construction ───────────────────────────────────────────────────
    def _build_window(self):
        win = Gtk.Window(title="HP Thermal Monitor")
        win.set_default_size(460, -1)
        win.set_resizable(False)
        win.connect("delete-event", lambda w, _: w.hide() or True)
        self._window = win

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(10); outer.set_margin_end(10)
        outer.set_margin_top(10);   outer.set_margin_bottom(10)
        win.add(outer)

        # ── Sensors ───────────────────────────────────────────────────────────
        sf = Gtk.Frame(label=" Sensors ")
        outer.pack_start(sf, False, False, 0)
        self._sensors_grid = Gtk.Grid()
        self._sensors_grid.set_column_spacing(20)
        self._sensors_grid.set_row_spacing(3)
        self._sensors_grid.set_margin_start(10); self._sensors_grid.set_margin_end(10)
        self._sensors_grid.set_margin_top(6);    self._sensors_grid.set_margin_bottom(6)
        sf.add(self._sensors_grid)

        # ── Runtime controls ──────────────────────────────────────────────────
        rf = Gtk.Frame(label=" Runtime Controls ")
        outer.pack_start(rf, False, False, 0)
        rg = Gtk.Grid()
        rg.set_column_spacing(16); rg.set_row_spacing(8)
        rg.set_margin_start(10); rg.set_margin_end(10)
        rg.set_margin_top(6);    rg.set_margin_bottom(6)
        rf.add(rg)

        # Turbo (runtime)
        lbl = Gtk.Label(label="Turbo Boost (runtime):")
        lbl.set_xalign(0)
        rg.attach(lbl, 0, 0, 1, 1)
        self._sw_turbo = Gtk.Switch()
        self._sw_turbo.connect("notify::active", self._on_turbo)
        rg.attach(self._sw_turbo, 1, 0, 1, 1)

        # RAPL slider
        lbl2 = Gtk.Label(label="CPU Power Limit (PL1):")
        lbl2.set_xalign(0)
        rg.attach(lbl2, 0, 1, 1, 1)
        rapl_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._rapl_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 5, 28, 1)
        self._rapl_scale.set_size_request(200, -1)
        self._rapl_scale.set_draw_value(False)
        for mark in (5, 10, 15, 20, 28):
            self._rapl_scale.add_mark(mark, Gtk.PositionType.BOTTOM, f"{mark}W" if mark in (5, 28) else None)
        self._rapl_scale.connect("value-changed", self._on_rapl)
        rapl_row.pack_start(self._rapl_scale, True, True, 0)
        self._rapl_lbl = Gtk.Label(label="--W")
        self._rapl_lbl.set_width_chars(5)
        rapl_row.pack_start(self._rapl_lbl, False, False, 0)
        rg.attach(rapl_row, 1, 1, 1, 1)

        # RAPL persistent checkbox
        rg.attach(Gtk.Label(), 0, 2, 1, 1)  # spacer
        self._cb_rapl_persist = Gtk.CheckButton(label="Make persistent (restore on boot)")
        self._cb_rapl_persist.connect("toggled", self._on_rapl_persist)
        rg.attach(self._cb_rapl_persist, 1, 2, 1, 1)

        # ── BIOS controls ─────────────────────────────────────────────────────
        bf = Gtk.Frame(label=" BIOS Settings (take effect after reboot) ")
        outer.pack_start(bf, False, False, 0)
        bg = Gtk.Grid()
        bg.set_column_spacing(16); bg.set_row_spacing(8)
        bg.set_margin_start(10); bg.set_margin_end(10)
        bg.set_margin_top(6);    bg.set_margin_bottom(6)
        bf.add(bg)

        lbl3 = Gtk.Label(label="Turbo Boost (persistent):")
        lbl3.set_xalign(0)
        bg.attach(lbl3, 0, 0, 1, 1)
        self._sw_bios_turbo = Gtk.Switch()
        self._sw_bios_turbo.connect("notify::active", self._on_bios_turbo)
        bg.attach(self._sw_bios_turbo, 1, 0, 1, 1)
        self._turbo_persist_lbl = Gtk.Label()
        self._turbo_persist_lbl.set_markup('<small><i>(via BIOS)</i></small>')
        self._turbo_persist_lbl.set_xalign(0)
        bg.attach(self._turbo_persist_lbl, 2, 0, 1, 1)

        lbl4 = Gtk.Label(label="Fan Always On (AC Power):")
        lbl4.set_xalign(0)
        bg.attach(lbl4, 0, 1, 1, 1)
        self._sw_fan_ac = Gtk.Switch()
        self._sw_fan_ac.connect("notify::active", self._on_fan_ac)
        bg.attach(self._sw_fan_ac, 1, 1, 1, 1)

        note = Gtk.Label()
        note.set_markup('<small><i>⚠  BIOS changes require a reboot to take effect</i></small>')
        note.set_xalign(0)
        bg.attach(note, 0, 2, 2, 1)

    # ── Sensor polling ────────────────────────────────────────────────────────
    def _poll(self):
        def _bg():
            data = gather_sensors()
            GLib.idle_add(self._refresh_ui, data)
        threading.Thread(target=_bg, daemon=True).start()
        return True  # repeat

    def _refresh_ui(self, data):
        # Tray label
        cpu = data.get("CPU Temperature", data.get("CPU Package", (None, 0)))[1]
        fan = data.get("CPU Fan", (None, 0))[1]
        self._ind.set_label(f"{cpu}°C  {fan}rpm", "000°C 0000rpm")

        # Sensors grid
        for child in self._sensors_grid.get_children():
            self._sensors_grid.remove(child)

        shown = []
        for key in self._SENSOR_ORDER:
            if key in data:
                shown.append(key)
        for key in sorted(data):
            if not key.startswith("_") and key not in shown:
                shown.append(key)

        for row, key in enumerate(shown):
            kind, val = data[key]
            name_lbl = Gtk.Label(label=f"{key}:")
            name_lbl.set_xalign(0)

            if kind == "fan":
                val_str = f"{val} RPM"
                color   = "#00aa00" if val > 0 else "#888888"
            else:
                val_str = f"{val} °C"
                color   = ("#cc0000" if val >= 85 else
                           "#e06000" if val >= 70 else
                           "#aa8800" if val >= 55 else
                           "#00aa00")

            val_lbl = Gtk.Label()
            val_lbl.set_markup(f'<span foreground="{color}"><b>{val_str}</b></span>')
            val_lbl.set_xalign(1)
            self._sensors_grid.attach(name_lbl, 0, row, 1, 1)
            self._sensors_grid.attach(val_lbl,  1, row, 1, 1)

        self._sensors_grid.show_all()

        # Controls (block signals)
        self._updating_ui = True
        self._sw_turbo.set_active(data.get("_turbo_on", False))
        pl1 = data.get("_rapl_w", 15)
        self._rapl_scale.set_value(pl1)
        self._rapl_lbl.set_text(f"{pl1} W")
        self._cb_rapl_persist.set_active(data.get("_rapl_persistent", False))
        self._sw_bios_turbo.set_active(data.get("_bios_turbo") == "Enable")
        self._sw_fan_ac.set_active(data.get("_bios_fan") == "Enable")
        method = data.get("_turbo_persist_method", "bios")
        self._turbo_persist_lbl.set_markup(
            f'<small><i>(via {method})</i></small>'
        )
        self._turbo_persist = method
        self._updating_ui = False

    # ── Control handlers ──────────────────────────────────────────────────────
    def _on_turbo(self, sw, _):
        if self._updating_ui:
            return
        threading.Thread(target=helper_turbo, args=(sw.get_active(),), daemon=True).start()

    def _on_rapl(self, scale):
        if self._updating_ui:
            return
        w = int(scale.get_value())
        self._rapl_lbl.set_text(f"{w} W")
        if self._rapl_timer:
            GLib.source_remove(self._rapl_timer)
        self._rapl_timer = GLib.timeout_add(600, self._write_rapl, w)

    def _write_rapl(self, watts):
        self._rapl_timer = None
        threading.Thread(target=helper_rapl, args=(watts,), daemon=True).start()
        return False

    def _on_rapl_persist(self, cb):
        if self._updating_ui:
            return
        if cb.get_active():
            w = int(self._rapl_scale.get_value())
            threading.Thread(
                target=run_helper, args=("tmpfiles-set", str(w * 1_000_000)), daemon=True
            ).start()
        else:
            threading.Thread(target=run_helper, args=("tmpfiles-unset",), daemon=True).start()

    def _on_bios_turbo(self, sw, _):
        if self._updating_ui:
            return
        value = "Enable" if sw.get_active() else "Disable"
        GLib.idle_add(self._bios_turbo_ask, sw, value)

    def _bios_turbo_ask(self, sw, value):
        """Ask whether user knows BIOS password; pick BIOS or GRUB path."""
        dlg = Gtk.MessageDialog(
            transient_for=None, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="How should Turbo Boost be persisted?",
        )
        dlg.format_secondary_text(
            "Do you know the BIOS admin password?\n\n"
            "• Yes → write BIOS setting (takes effect after reboot)\n"
            "• No  → add kernel parameter via GRUB (takes effect after reboot)"
        )
        dlg.set_keep_above(True)
        dlg.add_button("I know the BIOS password", 1)
        dlg.add_button("Use GRUB kernel parameter", 2)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        if resp == 1:
            self._turbo_persist = "BIOS"
            GLib.idle_add(self._bios_write, sw, BIOS_TURBO_ATTR, value)
        elif resp == 2:
            self._turbo_persist = "GRUB"
            no_turbo = "1" if value == "Disable" else "0"
            def _do():
                ok, err = run_helper("grub-turbo", no_turbo)
                if ok:
                    GLib.idle_add(
                        self._turbo_persist_lbl.set_markup,
                        '<small><i>(via GRUB)</i></small>',
                    )
                    GLib.idle_add(self._show_info,
                        "GRUB updated. Reboot for turbo change to take effect.")
                else:
                    GLib.idle_add(self._show_error, f"GRUB update failed:\n{err}")
                    GLib.idle_add(self._revert_switch, sw)
            threading.Thread(target=_do, daemon=True).start()
        else:
            self._revert_switch(sw)

    def _on_fan_ac(self, sw, _):
        if self._updating_ui:
            return
        value = "Enable" if sw.get_active() else "Disable"
        GLib.idle_add(self._bios_write, sw, BIOS_FAN_AC_ATTR, value)

    def _bios_write(self, sw, attr, value):
        pwd, remember = self._prompt_bios_password()
        if not pwd:
            self._revert_switch(sw)
            return
        def _do():
            ok, err = helper_bios_set(attr, value, pwd)
            if ok:
                # Only cache and store on success
                self._bios_password = pwd
                if remember:
                    keyring_store(pwd)
            else:
                # Wrong password — clear cache so next attempt re-prompts
                self._bios_password = None
                GLib.idle_add(self._show_error, f"BIOS write failed (wrong password?):\n{err}")
                GLib.idle_add(self._revert_switch, sw)
        threading.Thread(target=_do, daemon=True).start()

    def _prompt_bios_password(self):
        """Return (password, remember) from cache or dialog. Never stores on its own."""
        if self._bios_password:
            return self._bios_password, False  # already verified, no need to re-store
        parent = self._window if self._window.get_visible() else None
        dlg = BiosPasswordDialog(parent)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            pwd = dlg.password
            remember = dlg.remember
            dlg.destroy()
            return pwd, remember
        dlg.destroy()
        return None, False

    def _revert_switch(self, sw):
        self._updating_ui = True
        sw.set_active(not sw.get_active())
        self._updating_ui = False

    def _show_error(self, msg):
        dlg = Gtk.MessageDialog(
            transient_for=self._window, modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text=msg,
        )
        dlg.run(); dlg.destroy()

    def _show_info(self, msg):
        dlg = Gtk.MessageDialog(
            transient_for=self._window, modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK, text=msg,
        )
        dlg.run(); dlg.destroy()

    def _show_window(self):
        self._window.show_all()
        self._window.present()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    ThermalApplet().run()
