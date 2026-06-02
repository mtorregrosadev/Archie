#!/usr/bin/python3
"""
archie.py — La mascota flotant + eines de línia de comandes.

Modes (primer argument):
    (cap)            daemon: comprova cada 5 min i mostra la bombolla si cal
    demo             mostra una bombolla d'exemple que NO desapareix sola
    check            comprova ARA i mostra el resultat (o "tot OK") en una bombolla
    status           taula a la consola amb l'estat de tots els checks
    fix              Aplica silenciosament TOTES les optimitzacions bàsiques de cop
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

# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #
APP_ID = "dev.archie.Archie"
CHECK_INTERVAL = 60         # comprova cada minut (per a crítics)
INITIAL_DELAY = 30          # espera després d'arrencar (no molestar al login)
MIN_GAP = 15 * 60          # mínim entre suggeriments no crítics
SNOOZE = 60 * 60           # "Ara no" → silencia aquest tema 1 h

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
}


def run_fix_all_cli() -> int:
    color = sys.stdout.isatty()
    def c(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    print(c("1;38;5;183", "\n 🐱 Archie — Taller de reparació ràpida"))
    print(c("90", " Avaluant l'estat del sistema i aplicant solucions...\n"))

    # Demanem sudo per endavant perquè els fixes silenciosos que el necessitin
    # no s'encallin (o fallin directament) demanant contrasenyes invisibles.
    subprocess.run(["sudo", "-v"], check=False)

    try:
        results = monitor.evaluate_all(force=True)
    except FileNotFoundError:
        print("archie: no trobo archie_checks.yaml", file=sys.stderr)
        return 1

    state = State()
    fixed_count = 0
    failed_count = 0

    for chk, st in results:
        if st == monitor.ALERT and chk.fix:
            if state.is_blocked(chk.id):
                continue

            print(f" ⚙️  Arreglant: {c('1', chk.id)}", end="", flush=True)
            ok = monitor.run_fix(chk.fix, silent=True)
            
            if ok:
                print(f"\r {c('32', '✓')} {c('1', chk.id)} " + c("90", "(aplicat amb èxit)"))
                fixed_count += 1
                if chk.once:
                    state.mark_applied(chk.id)
            else:
                print(f"\r {c('31', '✗')} {c('1', chk.id)} " + c("31", "(ha fallat l'script)"))
                failed_count += 1

    print(f"\n {c('1;32', str(fixed_count) + ' problemes solucionats.')}")
    if failed_count:
        print(f" {c('31', str(failed_count) + ' han fallat (potser requereixen terminal complet o més temps).')}")
    print()
    return 0

def run_status_cli() -> int:
    color = sys.stdout.isatty()
    def c(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    try:
        results = monitor.evaluate_all(force=True)
    except FileNotFoundError:
        print("archie: no trobo archie_checks.yaml", file=sys.stderr)
        return 1

    print(c("1;38;5;183", "\n 🐱 Archie — estat del sistema") +
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
        monitor.SKIP: " (omès — eina no instal·lada)",
        monitor.ERROR: " (error / timeout)",
    }

    order = monitor.CATEGORY_ORDER + [k for k in by_cat if k not in monitor.CATEGORY_ORDER]
    counts = defaultdict(int)
    for cat in order:
        if cat not in by_cat:
            continue
        print(" " + c("1", CATEGORY_LABELS.get(cat, cat)))
        for chk, st in sorted(by_cat[cat], key=lambda x: x[0].order):
            counts[st] += 1
            code, glyph = sym[st]
            line = f"   {c(code, glyph)} {chk.id}"
            if st == monitor.ALERT:
                line += "  " + c("90", "→ ") + chk.message
            elif st in note:
                line += c("90", note[st])
            print(line)
        print()

    summary = (
        f"{c('31', str(counts[monitor.ALERT]) + ' alertes')}   "
        f"{c('32', str(counts[monitor.OK]) + ' OK')}   "
        f"{c('90', str(counts[monitor.SKIP]) + ' omesos')}"
    )
    if counts[monitor.ERROR]:
        summary += f"   {c('33', str(counts[monitor.ERROR]) + ' errors')}"
    print(" " + summary)
    if counts[monitor.ALERT]:
        print(c("90", " Consell: «archie check» ho mostra com a bombolla flotant.\n"))
    return 0


# --------------------------------------------------------------------------- #
#  gtk4-layer-shell s'ha de carregar ABANS que libwayland quan s'usa via GI.
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

        # -- cicle de vida ------------------------------------------------- #
        def do_activate(self) -> None:
            if self.win is None:
                self._build_ui()
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
            GLib.timeout_add_seconds(CHECK_INTERVAL, self._periodic_check)
            return GLib.SOURCE_REMOVE

        def _periodic_check(self) -> bool:
            self.run_checks()
            return GLib.SOURCE_CONTINUE

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
            self.fix_btn = Gtk.Button(label="Arregla-ho")
            self.fix_btn.add_css_class("fix")
            self.fix_btn.connect("clicked", self.on_fix)
            self.later_btn = Gtk.Button(label="Ara no")
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
            recent_shown = (time.time() - self.state.last_shown < MIN_GAP)
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
            # Afegim la capacitat d'ignorar el check que acaben de mostrar
            # i que l'usuari ha ignorat, perquè pugui passar al següent en la llista
            # sense bloquejar tota la cua d'alertes indefinidament.
            def is_blocked_extended(sid: str) -> bool:
                if is_blocked(sid):
                    return True
                # Si l'acabem de mostrar recentment (1 hora), passa al següent.
                # Excepcions: alertes crítiques (aquestes mai s'ignoren)
                if not only_critical and (time.time() - self._shown_times.get(sid, 0) < 3600):
                    return True
                return False

            try:
                check = monitor.first_alert(is_blocked_extended, force=force, only_critical=only_critical)
            except Exception as e:
                print(f"archie: error comprovant: {e}", file=sys.stderr)
                check = None
            GLib.idle_add(done_cb, check)

        def _sweep_done(self, check) -> bool:
            self._sweeping = False
            if check is not None and not self.showing:
                self._present(check, record=True)
            return False

        def _check_done(self, check) -> bool:
            self._sweeping = False
            if check is None:
                check = monitor.Check(
                    id="all_ok", category="info",
                    message="Tot sembla correcte ✓\nCap optimització pendent ara mateix.",
                    detect="", fix=None)
            self._present(check, record=False)
            return False

        # -- mostrar / amagar ---------------------------------------------- #
        def _present(self, check, record: bool) -> None:
            self.current = check
            self.showing = True
            self.msg_label.set_text(check.message)
            if check.id not in ("all_ok", "demo"):
                self._shown_times[check.id] = time.time()
            
            if check.fix:
                self.fix_btn.set_label(check.label or "Arregla-ho")
                self.fix_btn.set_visible(True)
                self.later_btn.set_label("Ara no")
            else:
                self.fix_btn.set_visible(False)
                self.later_btn.set_label("Entesos")

            if record and self.mode == "daemon":
                self.state.mark_shown(check.id)

            self._autohide_secs = (
                0 if self.mode == "demo"
                else CHECK_MODE_TIMEOUT if self.mode == "check"
                else DEFAULT_TIMEOUT
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
            demo = monitor.Check(
                id="demo", category="info",
                message="Hola! Sóc l'Archie 🐱\nAixí et donaré els suggeriments.\n(Això és una demo, no marxa sola.)",
                detect="", fix="true", label="Arregla-ho")
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
            if c is not None and self.mode != "demo" and c.fix:
                ok = monitor.run_fix(c.fix)
                if ok and c.once:
                    self.state.mark_applied(c.id)
                c._status = "unknown"  # Força reavaluació si torna a sortir
            self._hide()

        def on_later(self, _btn) -> None:
            self._cancel_autohide()
            c = self.current
            if (c is not None and self.mode != "demo"
                    and c.id not in ("all_ok", "demo")):
                self.state.snooze(c.id, SNOOZE)
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
        elif a in ("help", "h"):
            print(__doc__)
            return 0
        else:
            print(f"archie: mode desconegut '{args[0]}'. Prova: status, fix, demo, check, help",
                  file=sys.stderr)
            return 2

    if mode == "status":
        return run_status_cli()
    if mode == "fix":
        return run_fix_all_cli()

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return run_gui(mode)


if __name__ == "__main__":
    sys.exit(main())
