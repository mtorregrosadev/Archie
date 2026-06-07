#!/usr/bin/python3
"""
archie.py — La mascota flotant + eines de línia de comandes.

Modes (primer argument):
    (cap)            daemon: comprova periòdicament i mostra la bombolla si cal
    demo             mostra una bombolla d'exemple que NO desapareix sola
    check            comprova ARA i mostra el resultat (o "tot OK") en una bombolla
    status           taula a la consola amb l'estat de tots els checks
    brain            què ha mostrejat i après l'Archie de tu (transparència)
    undo             desfà totes les automatitzacions actives ara mateix
    reset [all]      esborra els snoozes (amb 'all', també aprenentatge i estat)
    fix              aplica silenciosament TOTES les optimitzacions bàsiques de cop
    help             aquesta ajuda

NOTA: shebang /usr/bin/python3 a propòsit. PyGObject (gi) viu a
/usr/lib/python3.x/site-packages i NO és visible des d'un Python de mise/pyenv.
El servei systemd també crida /usr/bin/python3.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict

import monitor
import brain
import dbus_listener
import asyncio

# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #
APP_ID = "dev.archie.Archie"
CHECK_INTERVAL = 60         # comprova cada minut (per a crítics)
INITIAL_DELAY = 30          # espera després d'arrencar (no molestar al login)
MIN_GAP = 15 * 60          # mínim entre suggeriments no crítics
GRACE_AFTER_FIX = 6 * 3600 # en fer "Arregla-ho", calla aquest tema 6 h (encara
                           # que el detect segueixi saltant: p. ex. cache que no
                           # baixa del llindar) per no convertir-se en un nag.
# El temps de silenci en fer "Ara no" ja no és fix: el calcula brain.snooze_for()
# amb backoff exponencial (com més ignores un tema, més estona calla).

DEFAULT_TIMEOUT = int(os.environ.get("ARCHIE_TIMEOUT", "20"))  # s visible (daemon)
CHECK_MODE_TIMEOUT = 30     # s visible en mode 'check'
FADE_IN_MS = 260
FADE_OUT_MS = 180

CAT = r""" /\_/\
( o.o )
 > ^ <"""

CSS = b"""
window { background: transparent; }

.bubble {
  background-color: #1e2030;
  border: 1px solid #363a4f;
  border-radius: 16px;
  padding: 14px 18px;
  margin: 6px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.55);
}

.cat {
  color: #c6a0f6;
  font-family: monospace;
  font-size: 14px;
  font-weight: bold;
}

.msg { color: #cad3f5; font-size: 13px; }
.archie-title { color: #c6a0f6; font-weight: bold; font-size: 13px; }

button.fix, button.later {
  border: none;
  background-image: none;
  box-shadow: none;
  border-radius: 10px;
  padding: 5px 14px;
  min-height: 0;
  font-size: 12px;
}
button.fix { background-color: #a6da95; color: #1e2030; font-weight: bold; }
button.fix:hover { background-color: #b6e6a6; }
button.later { background-color: #494d64; color: #cad3f5; }
button.later:hover { background-color: #5b6078; }
"""


# --------------------------------------------------------------------------- #
#  Estat persistent (JSON)
# --------------------------------------------------------------------------- #
STATE_DIR = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "archie"
)
STATE_FILE = os.path.join(STATE_DIR, "state.json")


class State:
    def __init__(self) -> None:
        self.data = {"last_shown": 0.0, "applied": {}, "snoozed": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (FileNotFoundError, ValueError, OSError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            print(f"archie: no he pogut desar l'estat: {e}", file=sys.stderr)

    @property
    def last_shown(self) -> float:
        return float(self.data.get("last_shown", 0.0))

    def mark_shown(self, sid: str = None) -> None:
        self.data["last_shown"] = time.time()
        if sid:
            self.data["last_shown_id"] = sid
            self.data["last_shown_time"] = time.time()
        self._save()

    def is_applied(self, sid: str) -> bool:
        return bool(self.data.get("applied", {}).get(sid))

    def mark_applied(self, sid: str) -> None:
        self.data.setdefault("applied", {})[sid] = True
        self._save()

    def is_snoozed(self, sid: str) -> bool:
        return float(self.data.get("snoozed", {}).get(sid, 0)) > time.time()

    def snooze(self, sid: str, seconds: int) -> None:
        self.data.setdefault("snoozed", {})[sid] = time.time() + seconds
        self._save()

    def is_blocked(self, sid: str) -> bool:
        return self.is_applied(sid) or self.is_snoozed(sid)


# --------------------------------------------------------------------------- #
#  Vista d'estat per consola (no necessita GTK)
# --------------------------------------------------------------------------- #
CATEGORY_LABELS = {
    "ca": {
        "thermals": "🌡️  Tèrmica",
        "cpu": "⚡ CPU & energia",
        "memory": "🧠 Memòria",
        "ssd": "💾 SSD & disc",
        "wayland": "🌐 Wayland vs X11",
        "boot": "🚀 Arrencada",
        "battery": "🔋 Bateria",
        "security": "🔒 Seguretat",
        "gpu": "🎮 GPU",
        "network": "🌐 Xarxa",
        "performance": "🖥️  Rendiment",
    },
    "en": {
        "thermals": "🌡️  Thermals",
        "cpu": "⚡ CPU & Power",
        "memory": "🧠 Memory",
        "ssd": "💾 SSD & Disk",
        "wayland": "🌐 Wayland vs X11",
        "boot": "🚀 Boot",
        "battery": "🔋 Battery",
        "security": "🔒 Security",
        "gpu": "🎮 GPU",
        "network": "🌐 Network",
        "performance": "🖥️  Performance",
    }
}

def _T(ca: str, en: str) -> str:
    return en if os.environ.get("ARCHIE_LANG", "ca") == "en" else ca



def run_fix_all_cli() -> int:
    color = sys.stdout.isatty()
    def c(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    print(c("1;38;5;183", _T("\n 🐱 Archie — Taller de reparació ràpida", "\n 🐱 Archie — Quick Repair Shop")))
    print(c("90", _T(" Avaluant l'estat del sistema i aplicant solucions...\n", " Evaluating system state and applying solutions...\n")))

    subprocess.run(["sudo", "-v"], check=False)

    try:
        results = monitor.evaluate_all(force=True)
    except FileNotFoundError:
        print(_T("archie: no trobo archie_checks.yaml", "archie: could not find archie_checks.yaml"), file=sys.stderr)
        return 1

    state = State()
    fixed_count = 0
    failed_count = 0

    for chk, st in results:
        if st == monitor.ALERT and chk.run_command:
            if state.is_blocked(chk.id):
                continue

            print(f" ⚙️  {_T('Arreglant:', 'Fixing:')} {c('1', chk.id)}", end="", flush=True)
            ok = monitor.run_fix(chk.run_command, silent=True)
            
            if ok:
                print(f"\r {c('32', '✓')} {c('1', chk.id)} " + c("90", _T("(aplicat amb èxit)", "(successfully applied)")))
                fixed_count += 1
                if chk.once:
                    state.mark_applied(chk.id)
            else:
                print(f"\r {c('31', '✗')} {c('1', chk.id)} " + c("31", _T("(ha fallat l'script)", "(script failed)")))
                failed_count += 1

    print(f"\n {c('1;32', str(fixed_count) + _T(' problemes solucionats.', ' problems fixed.'))}")
    if failed_count:
        print(f" {c('31', str(failed_count) + _T(' han fallat (potser requereixen terminal complet o més temps).', ' failed (might require full terminal or more time).'))}")
    print()
    return 0

def run_status_cli() -> int:
    color = sys.stdout.isatty()
    lang = os.environ.get("ARCHIE_LANG", "ca")
    def c(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    try:
        results = monitor.evaluate_all(force=True)
    except FileNotFoundError:
        print(_T("archie: no trobo archie_checks.yaml", "archie: could not find archie_checks.yaml"), file=sys.stderr)
        return 1

    print(c("1;38;5;183", _T("\n 🐱 Archie — estat del sistema", "\n 🐱 Archie — system status")) +
          c("90", f"   ({time.strftime('%Y-%m-%d %H:%M')})\n"))

    by_cat = defaultdict(list)
    for chk, st in results:
        by_cat[chk.category].append((chk, st))

    sym = {
        monitor.ALERT: ("31", "✗"),
        monitor.OK: ("32", "✓"),
        monitor.SKIP: ("90", "·"),
        monitor.ERROR: ("33", "‼"),
    }
    note = {
        monitor.SKIP: _T(" (omès — eina no instal·lada)", " (skipped — tool not installed)"),
        monitor.ERROR: _T(" (error / timeout)", " (error / timeout)"),
    }

    order = monitor.CATEGORY_ORDER + [k for k in by_cat if k not in monitor.CATEGORY_ORDER]
    counts = defaultdict(int)
    for cat in order:
        if cat not in by_cat:
            continue
        print(" " + c("1", CATEGORY_LABELS[lang].get(cat, cat)))
        for chk, st in sorted(by_cat[cat], key=lambda x: x[0].order):
            counts[st] += 1
            code_c, glyph = sym[st]
            line = f"   {c(code_c, glyph)} {chk.id}"
            if st == monitor.ALERT:
                line += "  " + c("90", "→ ") + chk.display_message
            elif st in note:
                line += c("90", note[st])
            print(line)
        print()

    summary = (
        f"{c('31', str(counts[monitor.ALERT]) + _T(' alertes', ' alerts'))}   "
        f"{c('32', str(counts[monitor.OK]) + ' OK')}   "
        f"{c('90', str(counts[monitor.SKIP]) + _T(' omesos', ' skipped'))}"
    )
    if counts[monitor.ERROR]:
        summary += f"   {c('33', str(counts[monitor.ERROR]) + _T(' errors', ' errors'))}"
    print(" " + summary)
    if counts[monitor.ALERT]:
        print(c("90", _T(" Consell: «archie check» ho mostra com a bombolla flotant.\n", " Tip: «archie check» shows it as a floating bubble.\n")))
    return 0

def run_brain_cli() -> int:
    """Transparència: què ha mostrejat i après l'Archie de tu."""
    color = sys.stdout.isatty()
    def c(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    info = brain.Brain(State()).insights()
    print(c("1;38;5;183", _T("\n 🧠 Archie — què sé de tu", "\n 🧠 Archie — what I know about you")))

    bat = info["battery"]
    pct = f"{bat['pct']}%" if bat["pct"] is not None else "—"
    estat = _T("🔋 amb bateria", "🔋 on battery") if bat["on_battery"] else _T("🔌 endollat", "🔌 plugged in")
    if bat.get("eta_min"):
        estat += f" (~{bat['eta_min']} min)"
    repos = _T("en repòs", "idle") if info["idle"] else _T("actiu", "active")
    loads = ", ".join(f"{l:.2f}" for l in info["loads"]) or "—"
    print(c("90", f"   {estat} · {_T('bateria', 'battery')} {pct} · {repos} · "
                  f"{info['samples']} {_T('mostres', 'samples')} · {_T('càrrega recent', 'recent load')} [{loads}]"))
    if info.get("focus"):
        print(c("33", _T("   🎬 Mode focus actiu: ara mateix només t'avisaria de coses crítiques.", "   🎬 Focus mode active: right now I would only warn you about critical things.")))
    print()

    ghosts = info["ghosts"]
    print(" " + c("1", _T("👻 Accions actives ara mateix", "👻 Currently active actions")))
    if not ghosts:
        print(c("90", _T("   (cap — no he canviat res del teu sistema)", "   (none — I haven't changed anything on your system)")))
    for g in ghosts:
        tag = c("36", _T("[après]", "[learned]")) if g["learned"] else c("35", _T("[auto-desfà]", "[auto-undo]") if g["auto_restore"] else _T("[manual]", "[manual]"))
        print(f"   • {tag} {g['msg']}")
    print(c("90", _T("   Pots desfer-ho tot amb «archie undo».\n", "   You can undo everything with «archie undo».\n")))

    habits = info["habits"]
    print(" " + c("1", _T("📚 Hàbits apresos", "📚 Learned habits")))
    if not habits:
        print(c("90", _T("   (encara no he après res; t'aniré coneixent)", "   (I haven't learned anything yet; I'll get to know you)")))
    for h in habits:
        a, d = h["accept"], h["dismiss"]
        if h["auto_disabled"]:
            mark = c("31", _T("ho faig sol: NO (ho vas desfer)", "auto apply: NO (you undid it)"))
        elif h["auto"] and h["streak_accept"] >= 3:
            mark = c("32", _T("ho faig sol ✓", "auto apply ✓"))
        elif h["auto"]:
            falten = max(0, 3 - h["streak_accept"])
            mark = c("33", f"{_T('a', 'needs')} {falten} {_T('accepts de fer-ho sol', 'accepts to auto apply')}") if falten else c("32", _T("ho faig sol ✓", "auto apply ✓"))
        else:
            mark = c("90", _T("només suggeriment", "suggestion only"))
        print(f"   • {c('1', h['id'])}  "
              + c("32", f"✓{a}") + " " + c("31", f"✗{d}")
              + (c("90", f"  ({_T('ignorat', 'ignored')} {h['streak_dismiss']}× {_T('seguits', 'in a row')})") if h["streak_dismiss"] else "")
              + f"  — {mark}")
    print()
    return 0

def run_undo_cli() -> int:
    done = brain.Brain(State()).undo_all()
    if not done:
        print(_T("archie: no hi ha cap acció activa per desfer.", "archie: no active action to undo."))
        return 0
    for gid, msg in done:
        print(f"  ↩  {gid}  {msg}")
    print(_T(f"\narchie: {len(done)} acció(ns) desfeta(es).", f"\narchie: {len(done)} action(s) undone."))
    return 0

def run_reset_cli() -> int:
    hard = len(sys.argv) > 2 and sys.argv[2].lower() in ("all", "--all", "hard", "tot")
    st = State()
    brain.Brain(st).undo_all()
    st.data["snoozed"] = {}
    if hard:
        st.data["applied"] = {}
        st.data["feedback"] = {}
        st.data["history"] = []
        st.data.pop("last_ghost_time", None)
    st._save()
    if hard:
        print(_T("archie: estat reiniciat DEL TOT (snoozes, optimitzacions i aprenentatge).", "archie: state reset COMPLETELY (snoozes, optimizations and learning)."))
    else:
        print(_T("archie: snoozes esborrats (l'aprenentatge es manté). Usa «archie reset all» per esborrar-ho tot.", "archie: snoozes cleared (learning is kept). Use «archie reset all» to clear everything."))
    return 0

def run_config_cli() -> int:
    import curses

    def draw_menu(stdscr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, -1, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)

        stdscr.bkgd(' ', curses.color_pair(1))

        state = State()
        config = state.data.get("config", {})
        
        options = [
            {"key": "lang", "label_ca": "Idioma", "label_en": "Language", "choices": ["ca", "en"], "default": "ca"},
            {"key": "check_interval", "label_ca": "Interval de comprovació (segons)", "label_en": "Check interval (seconds)", "choices": [30, 60, 120, 300], "default": 60},
            {"key": "min_gap", "label_ca": "Temps mínim entre avisos (minuts)", "label_en": "Minimum gap between alerts (minutes)", "choices": [5, 10, 15, 30, 60], "default": 15},
            {"key": "timeout", "label_ca": "Temps visible de la bombolla (segons)", "label_en": "Bubble visible time (seconds)", "choices": [10, 20, 30, 0], "default": 20},
        ]
        
        current_row = 0

        while True:
            lang = config.get("lang", "ca")
            stdscr.clear()
            
            title = "🐱 Archie - Configuration" if lang == "en" else "🐱 Archie - Configuració"
            stdscr.addstr(1, 2, title, curses.color_pair(2) | curses.A_BOLD)
            
            inst1 = "Use the arrows (↑/↓) to navigate and (←/→) to change values." if lang == "en" else "Utilitza les fletxes (↑/↓) per moure't i (←/→) per canviar els valors."
            inst2 = "Press [Enter] to Save and Exit, or [Esc] to cancel." if lang == "en" else "Prem [Enter] per Desar i Sortir, o [Esc] per cancel·lar."
            
            stdscr.addstr(2, 2, inst1, curses.color_pair(1))
            stdscr.addstr(3, 2, inst2, curses.color_pair(1))
            
            for idx, opt in enumerate(options):
                x = 4
                y = 6 + idx * 2
                val = config.get(opt["key"], opt["default"])
                
                if opt["key"] == "timeout" and val == 0:
                    val_str = "Infinite" if lang == "en" else "Infinit"
                elif opt["key"] == "lang":
                    val_str = "English" if val == "en" else "Català"
                else:
                    val_str = str(val)
                
                label = opt["label_en"] if lang == "en" else opt["label_ca"]
                label_str = f"{label}: < {val_str} >"
                
                if idx == current_row:
                    stdscr.addstr(y, x, label_str, curses.color_pair(3) | curses.A_BOLD)
                else:
                    stdscr.addstr(y, x, label_str, curses.color_pair(1))
            
            stdscr.refresh()
            key = stdscr.getch()
            
            if key == curses.KEY_UP and current_row > 0:
                current_row -= 1
            elif key == curses.KEY_DOWN and current_row < len(options) - 1:
                current_row += 1
            elif key == curses.KEY_LEFT:
                opt = options[current_row]
                val = config.get(opt["key"], opt["default"])
                try:
                    idx = opt["choices"].index(val)
                    if idx > 0:
                        config[opt["key"]] = opt["choices"][idx - 1]
                except ValueError:
                    config[opt["key"]] = opt["choices"][0]
            elif key == curses.KEY_RIGHT:
                opt = options[current_row]
                val = config.get(opt["key"], opt["default"])
                try:
                    idx = opt["choices"].index(val)
                    if idx < len(opt["choices"]) - 1:
                        config[opt["key"]] = opt["choices"][idx + 1]
                except ValueError:
                    config[opt["key"]] = opt["choices"][-1]
            elif key in [10, 13]: # Enter
                state.data["config"] = config
                state._save()
                return True
            elif key == 27: # Esc
                return False
                
    try:
        saved = curses.wrapper(draw_menu)
        lang = State().data.get("config", {}).get("lang", "ca")
        if saved:
            if lang == "en":
                print("\n 🐱 Configuration saved and applied successfully.")
                print(" Restart the Archie service to apply changes: systemctl --user restart archie")
            else:
                print("\n 🐱 Configuració guardada i aplicada correctament.")
                print(" Reinicia el servei de l'Archie per aplicar els canvis: systemctl --user restart archie")
        else:
            if lang == "en":
                print("\n ❌ Configuration cancelled.")
            else:
                print("\n ❌ Configuració cancel·lada.")
    except Exception as e:
        print(f"\n Error: {e}")
    return 0


# --------------------------------------------------------------------------- #
#  gtk4-layer-shell s'ha de carregar ABANS que libwayland quand s'usa via GI.
# --------------------------------------------------------------------------- #
def _ensure_layer_shell_preloaded() -> None:
    if os.environ.get("ARCHIE_NO_PRELOAD") == "1":
        return
    current = os.environ.get("LD_PRELOAD", "")
    if "gtk4-layer-shell" in current:
        return
    for lib in (
        "/usr/lib/libgtk4-layer-shell.so",
        "/usr/lib64/libgtk4-layer-shell.so",
        "/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so",
        "/lib/libgtk4-layer-shell.so",
    ):
        if os.path.exists(lib):
            os.environ["LD_PRELOAD"] = lib + (":" + current if current else "")
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except OSError:
                pass
            return


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
def run_gui(mode: str) -> int:
    _ensure_layer_shell_preloaded()

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

    try:
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell as LayerShell
        have_layer = True
    except (ValueError, ImportError):
        LayerShell = None
        have_layer = False
        print("archie: gtk4-layer-shell no disponible; la finestra no flotarà.",
              file=sys.stderr)

    class ArchieApp(Gtk.Application):
        def __init__(self, mode: str, **kw) -> None:
            super().__init__(**kw)
            self.mode = mode
            self.state = State()
            self.win = None
            self.bubble = None
            self.msg_label = None
            self.fix_btn = None
            self.later_btn = None
            self.current = None
            self.showing = False
            self._sweeping = False
            self._autohide_id = 0
            self._autohide_secs = 0
            self._scheduled = False
            self._shown_times = {}
            self.dbus = None

        def _start_dbus(self) -> None:
            """Starts the D-Bus listener in a separate event loop."""
            async def run_dbus():
                import monitor
                monitor.DBUS_ACTIVE = True
                b = brain.Brain(self.state)
                self.dbus = dbus_listener.DBusListener(b.handle_dbus_event)
                await self.dbus.start()
            
            try:
                asyncio.run(run_dbus())
            except Exception as e:
                print(f"archie: dbus error: {e}", file=sys.stderr)

        def _get_config(self, key: str, default_value: int) -> int:
            return self.state.data.get("config", {}).get(key, default_value)

        # -- cicle de vida ------------------------------------------------- #
        def do_activate(self) -> None:
            if self.win is None:
                self._build_ui()
            if self.dbus is None:
                threading.Thread(target=self._start_dbus, daemon=True).start()
            self.hold()
            if self.mode == "daemon":
                if not self._scheduled:
                    self._scheduled = True
                    GLib.timeout_add_seconds(INITIAL_DELAY, self._initial_check)
            elif self.mode == "demo":
                self._show_demo()
            elif self.mode == "check":
                self._run_once()

        def _initial_check(self) -> bool:
            self.run_checks()
            GLib.timeout_add_seconds(self._get_config("check_interval", CHECK_INTERVAL), self._periodic_check)
            return GLib.SOURCE_REMOVE

        def _periodic_check(self) -> bool:
            self.run_checks()
            GLib.timeout_add_seconds(self._get_config("check_interval", CHECK_INTERVAL), self._periodic_check)
            return GLib.SOURCE_REMOVE

        # -- UI ------------------------------------------------------------ #
        def _build_ui(self) -> None:
            provider = Gtk.CssProvider()
            try:
                provider.load_from_string(CSS.decode("utf-8"))  # GTK >= 4.12
            except (AttributeError, TypeError):
                provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            self.win = Gtk.ApplicationWindow(application=self)
            self.win.set_decorated(False)
            self.win.set_resizable(False)

            if have_layer:
                LayerShell.init_for_window(self.win)
                LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
                LayerShell.set_anchor(self.win, LayerShell.Edge.BOTTOM, True)
                LayerShell.set_anchor(self.win, LayerShell.Edge.RIGHT, True)
                LayerShell.set_margin(self.win, LayerShell.Edge.BOTTOM, 24)
                LayerShell.set_margin(self.win, LayerShell.Edge.RIGHT, 24)
                LayerShell.set_keyboard_mode(self.win,
                                             LayerShell.KeyboardMode.NONE)
                try:
                    LayerShell.set_namespace(self.win, "archie")
                except Exception:
                    pass

            self.bubble = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            self.bubble.add_css_class("bubble")

            cat = Gtk.Label(label=CAT)
            cat.add_css_class("cat")
            cat.set_valign(Gtk.Align.CENTER)
            self.bubble.append(cat)

            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            right.set_valign(Gtk.Align.CENTER)

            title = Gtk.Label(label="Archie")
            title.add_css_class("archie-title")
            title.set_xalign(0)
            right.append(title)

            self.msg_label = Gtk.Label()
            self.msg_label.add_css_class("msg")
            self.msg_label.set_wrap(True)
            self.msg_label.set_max_width_chars(34)
            self.msg_label.set_xalign(0)
            right.append(self.msg_label)

            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btns.set_halign(Gtk.Align.END)
            lang = os.environ.get("ARCHIE_LANG", "ca")
            self.fix_btn = Gtk.Button(label="Fix it" if lang == "en" else "Arregla-ho")
            self.fix_btn.add_css_class("fix")
            self.fix_btn.connect("clicked", self.on_fix)
            self.later_btn = Gtk.Button(label="Not now" if lang == "en" else "Ara no")
            self.later_btn.add_css_class("later")
            self.later_btn.connect("clicked", self.on_later)
            btns.append(self.fix_btn)
            btns.append(self.later_btn)
            right.append(btns)

            self.bubble.append(right)
            self.win.set_child(self.bubble)

            # Pausa l'auto-hide mentre el ratolí és a sobre.
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", lambda *a: self._cancel_autohide())
            motion.connect("leave", lambda *a: self._arm_autohide())
            self.bubble.add_controller(motion)

            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self._on_key)
            self.win.add_controller(keys)

        # -- comprovació (daemon) ------------------------------------------ #
        def run_checks(self) -> None:
            if self.showing or self._sweeping:
                return
            # Purga entrades velles perquè el dict no creixi sense límit.
            cutoff = time.time() - 7200
            self._shown_times = {k: v for k, v in self._shown_times.items() if v > cutoff}
            
            min_gap_secs = self._get_config("min_gap", MIN_GAP // 60) * 60
            recent_shown = (time.time() - self.state.last_shown < min_gap_secs)
            
            self._sweeping = True
            threading.Thread(
                target=self._sweep_worker,
                args=(self.state.is_blocked, False, recent_shown, self._sweep_done),
                daemon=True,
            ).start()

        def _run_once(self) -> None:
            self._sweeping = True
            threading.Thread(
                target=self._sweep_worker,
                args=(lambda _i: False, True, False, self._check_done),
                daemon=True,
            ).start()

        def _sweep_worker(self, is_blocked, force, only_critical, done_cb) -> None:
            b = brain.Brain(self.state)
            # Mostreig de tendències (bateria, càrrega, RAM, processos) cada cicle.
            b.sample()

            # Mode focus: si estàs gravant/en reunió o a pantalla completa, només
            # deixem passar el que és crític (sobreescalfament, OOM…).
            if not force and not only_critical and b.in_focus_mode():
                only_critical = True

            # 1. Intel·ligència proactiva: accions fantasma reversibles i
            #    auto-restauració quand canvia el context (només en mode daemon).
            if not force and not only_critical:
                event = b.tick()
                if event is not None:
                    check = monitor.Check(
                        id=event["id"], category="ghost",
                        message=event["message"], detect="",
                        fix=event.get("undo"),
                        label=event.get("undo_label", "Desfés"))
                    check._is_ghost = True
                    check._ghost_id = event["id"]
                    check._ghost_restore = (event.get("kind") == "restore")
                    GLib.idle_add(done_cb, check)
                    return

            # Ignora temporalment el que acabem de mostrar (1 h) perquè la cua
            # avanci; els crítics no s'ignoren mai.
            def is_blocked_extended(sid: str) -> bool:
                if is_blocked(sid):
                    return True
                if not only_critical and (time.time() - self._shown_times.get(sid, 0) < 3600):
                    return True
                return False

            try:
                check = monitor.first_alert(is_blocked_extended, force=force,
                                            only_critical=only_critical)
            except Exception as e:
                print(f"archie: error comprovant: {e}", file=sys.stderr)
                check = None

            # 2. Anomalies que només es veuen amb tendències (fuga de memòria,
            #    drenatge ràpid). Tenen prioritat sobre els suggeriments normals,
            #    però mai sobre una alerta crítica.
            if not force and not only_critical and (check is None or not check.critical):
                syn = b.leak_suspect() or b.drain_suspect()
                if (syn is not None and not self.state.is_blocked(syn["id"])
                        and time.time() - self._shown_times.get(syn["id"], 0) >= 3600):
                    vc = monitor.Check(
                        id=syn["id"], category=syn.get("category", "memory"),
                        message=syn["message"], detect="",
                        fix=syn.get("fix"), label=syn.get("label", "Arregla-ho"))
                    GLib.idle_add(done_cb, vc)
                    return

            # 3. Automatització apresa: si sempre acceptes un fix segur i
            #    reversible, l'Archie el fa sol i només t'ofereix desfer-ho.
            if (check is not None and not force and not only_critical
                    and b.should_autoapply(check)):
                monitor.run_fix(check.run_command, silent=True)
                b.mark_autoapplied(check)
                lang = os.environ.get("ARCHIE_LANG", "ca")
                suffix = "\n(Ho he fet jo sol perquè sempre ho acceptes. «Desfés» si no toca.)" if lang != "en" else "\n(I did it automatically because you always accept it. «Undo» if not appropriate.)"
                vc = monitor.Check(
                    id=check.id, category="ghost",
                    message=f"🤖 {check.display_message}{suffix}",
                    detect="", fix=check.undo, label="Desfés" if lang != "en" else "Undo")
                vc._is_ghost = True
                vc._ghost_id = check.id
                vc._learned = True
                GLib.idle_add(done_cb, vc)
                return

            GLib.idle_add(done_cb, check)

        def _sweep_done(self, check) -> bool:
            self._sweeping = False
            if check is not None and not self.showing:
                self._present(check, record=True)
            return False

        def _check_done(self, check) -> bool:
            self._sweeping = False
            if check is None:
                lang = os.environ.get("ARCHIE_LANG", "ca")
                msg = "Tot sembla correcte ✓\nCap optimització pendent ara mateix." if lang != "en" else "Everything looks good ✓\nNo pending optimizations right now."
                check = monitor.Check(
                    id="all_ok", category="info",
                    message=msg,
                    detect="", fix=None)
            self._present(check, record=False)
            return False

        # -- mostrar / amagar ---------------------------------------------- #
        def _present(self, check, record: bool) -> None:
            self.current = check
            self.showing = True
            self.msg_label.set_text(check.display_message)
            if check.id not in ("all_ok", "demo"):
                self._shown_times[check.id] = time.time()
            
            is_ghost = getattr(check, "_is_ghost", False)
            
            lang = os.environ.get("ARCHIE_LANG", "ca")
            if check.fix:
                self.fix_btn.set_label(getattr(check, "display_label", check.label) or ("Fix it" if lang == "en" else "Arregla-ho"))
                self.fix_btn.set_visible(True)
                if is_ghost:
                    self.later_btn.set_visible(False)
                else:
                    self.later_btn.set_visible(True)
                    self.later_btn.set_label("Not now" if lang == "en" else "Ara no")
            else:
                self.fix_btn.set_visible(False)
                self.later_btn.set_visible(True)
                self.later_btn.set_label("Got it" if lang == "en" else "Entesos")

            if record and self.mode == "daemon" and not is_ghost:
                self.state.mark_shown(check.id)

            self._autohide_secs = (
                0 if self.mode == "demo"
                else CHECK_MODE_TIMEOUT if self.mode == "check"
                # Avisos de restauració (només informatius) curts; les accions
                # amb "Desfés" duren el normal perquè hi puguis reaccionar.
                else 5 if getattr(check, "_ghost_restore", False) else self._get_config("timeout", DEFAULT_TIMEOUT)
            )

            self.bubble.set_opacity(0.0)
            self.win.present()
            self._animate(self.bubble, 0.0, 1.0, FADE_IN_MS)
            self._arm_autohide()

        def _run_chained_check(self) -> None:
            if self.showing or self._sweeping:
                return
            
            # Temps de respir i validació. Esperem 3 segons abans de llançar la següent.
            def delayed_sweep() -> bool:
                if not self.showing and not self._sweeping:
                    self._sweeping = True
                    threading.Thread(
                        target=self._sweep_worker,
                        args=(self.state.is_blocked, False, False, self._sweep_done),
                        daemon=True,
                    ).start()
                return GLib.SOURCE_REMOVE

            GLib.timeout_add_seconds(3, delayed_sweep)

        def _show_demo(self) -> None:
            lang = os.environ.get("ARCHIE_LANG", "ca")
            msg = "Hola! Sóc l'Archie 🐱\nAixí et donaré els suggeriments.\n(Això és una demo, no marxa sola.)" if lang != "en" else "Hi! I'm Archie 🐱\nThis is how I will give you suggestions.\n(This is a demo, it doesn't leave on its own.)"
            demo = monitor.Check(
                id="demo", category="info",
                message=msg,
                detect="", fix="true", label="Arregla-ho" if lang != "en" else "Fix it")
            self._present(demo, record=False)

        def _hide(self) -> None:
            self._cancel_autohide()

            def done() -> None:
                if self.win is not None:
                    self.win.set_visible(False)
                self.showing = False
                self.current = None
                if self.mode in ("demo", "check"):
                    self.quit()
                elif self.mode == "daemon":
                    # Immediatament busca el següent problema per fer cua
                    self._run_chained_check()

            start = self.bubble.get_opacity() if self.bubble else 1.0
            self._animate(self.bubble, start, 0.0, FADE_OUT_MS, on_done=done)

        def _arm_autohide(self) -> None:
            self._cancel_autohide()
            if not self.showing or self._autohide_secs <= 0:
                return
            self._autohide_id = GLib.timeout_add_seconds(
                self._autohide_secs, self._on_autohide)

        def _cancel_autohide(self) -> None:
            if self._autohide_id:
                GLib.source_remove(self._autohide_id)
                self._autohide_id = 0

        def _on_autohide(self) -> bool:
            self._autohide_id = 0
            self._hide()
            return GLib.SOURCE_REMOVE

        # -- botons -------------------------------------------------------- #
        def on_fix(self, _btn) -> None:
            self._cancel_autohide()
            c = self.current
            if c is not None and self.mode != "demo" and c.run_command:
                is_ghost = getattr(c, "_is_ghost", False)
                ok = monitor.run_fix(c.run_command, silent=is_ghost)
                if is_ghost:
                    # En ghost, "Arregla-ho" és en realitat "Desfés".
                    bn = brain.Brain(self.state)
                    if getattr(c, "_learned", False):
                        bn.record_feedback(c.id, accepted=False)  # no ho automatitzis més
                    bn.clear_ghost(getattr(c, "_ghost_id", c.id))
                else:
                    if ok and c.once:
                        self.state.mark_applied(c.id)
                    # Període de gràcia: ja hi has actuat, no insisteixis aviat
                    # encara que el detect torni a saltar.
                    self.state.snooze(c.id, GRACE_AFTER_FIX)
                    brain.Brain(self.state).record_feedback(c.id, accepted=True)
                c._status = "unknown"  # força reavaluació si torna a sortir
            self._hide()

        def on_later(self, _btn) -> None:
            self._cancel_autohide()
            c = self.current
            if c is not None and self.mode != "demo":
                is_ghost = getattr(c, "_is_ghost", False)
                if not is_ghost and c.id not in ("all_ok", "demo"):
                    bn = brain.Brain(self.state)
                    bn.record_feedback(c.id, accepted=False)
                    self.state.snooze(c.id, bn.snooze_for(c.id))  # backoff adaptatiu
            self._hide()

        def _on_key(self, _ctrl, keyval, _code, _mods) -> bool:
            if keyval == Gdk.KEY_Escape:
                self.on_later(None)
                return True
            return False

        # -- animació ------------------------------------------------------ #
        @staticmethod
        def _animate(widget, start, end, duration_ms, on_done=None) -> None:
            if widget is None:
                if on_done:
                    on_done()
                return
            clock = widget.get_frame_clock()
            if clock is None:
                widget.set_opacity(end)
                if on_done:
                    on_done()
                return
            t0 = clock.get_frame_time()

            def tick(w, fclock):
                progress = (fclock.get_frame_time() - t0) / 1000.0 / duration_ms
                if progress >= 1.0:
                    w.set_opacity(end)
                    if on_done:
                        on_done()
                    return GLib.SOURCE_REMOVE
                eased = 1 - (1 - progress) * (1 - progress)
                w.set_opacity(start + (end - start) * eased)
                return GLib.SOURCE_CONTINUE

            widget.add_tick_callback(tick)

    if mode in ("demo", "check"):
        app = ArchieApp(mode, application_id=f"{APP_ID}.{mode}",
                        flags=Gio.ApplicationFlags.NON_UNIQUE)
    else:
        app = ArchieApp(mode, application_id=APP_ID,
                        flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    return app.run(None)


# --------------------------------------------------------------------------- #
#  Entrada
# --------------------------------------------------------------------------- #
def main() -> int:
    state = State()
    os.environ["ARCHIE_LANG"] = state.data.get("config", {}).get("lang", "ca")
    args = sys.argv[1:]
    mode = "daemon"
    if args:
        a = args[0].lstrip("-").lower()
        if a in ("status", "st"):
            mode = "status"
        elif a == "demo":
            mode = "demo"
        elif a in ("check", "test", "once"):
            mode = "check"
        elif a == "fix":
            mode = "fix"
        elif a in ("brain", "insights", "memoria", "mem"):
            mode = "brain"
        elif a in ("undo", "desfes", "restore"):
            mode = "undo"
        elif a == "reset":
            mode = "reset"
        elif a in ("config", "conf", "c"):
            mode = "config"
        elif a in ("help", "h"):
            print(__doc__)
            return 0
        else:
            print(f"archie: mode desconegut '{args[0]}'. "
                  "Prova: status, brain, undo, reset, fix, demo, check, config, help",
                  file=sys.stderr)
            return 2

    if mode == "status":
        return run_status_cli()
    if mode == "brain":
        return run_brain_cli()
    if mode == "undo":
        return run_undo_cli()
    if mode == "reset":
        return run_reset_cli()
    if mode == "fix":
        return run_fix_all_cli()
    if mode == "config":
        return run_config_cli()

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return run_gui(mode)


if __name__ == "__main__":
    sys.exit(main())
