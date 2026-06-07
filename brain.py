"""brain.py — La capa d'intel·ligència de l'Archie.

No és GTK ni depèn de res extern: només llegeix sysfs i crida eines del sistema.

Tres responsabilitats:

  1. MOSTREIG (sample): cada cicle recull mètriques barates —bateria, càrrega,
     RAM, perfil d'energia— i en guarda un historial curt. Així pot raonar amb
     TENDÈNCIES (estàs realment en repòs? la bateria baixa?) i no amb una sola
     instantània que pot enganyar.

  2. APRENENTATGE (record_feedback): recorda què acceptes i què ignores. Com més
     ignores una cosa, més espai et deixa (backoff exponencial). Com més
     l'acceptes, més a prop està que l'Archie la faci SOL.

  3. ACCIONS FANTASMA (tick): automatitzacions contextuals i SEMPRE REVERSIBLES
     (perfil d'energia, Bluetooth, lluentor, animacions). S'apliquen soles quan
     toca i —el més important— es DESFAN soles quan el context canvia (p. ex.
     endolles el carregador). Cada acció guarda com desfer-se exactament.

Tot l'estat viu dins el mateix `state.json` que fa servir archie.py, via la
classe State compartida.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
import signal
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import monitor

import ctypes
import ctypes.util

# Carreguem libc per a crides directes al sistema (eficiència extrema)
_libc = ctypes.CDLL(ctypes.util.find_library("c"))

def _set_priority(pid: int, priority: int) -> bool:
    """Canvia la prioritat (nice) d'un procés via libc directament."""
    # setpriority(PRIO_PROCESS=0, who, priority)
    try:
        return _libc.setpriority(0, pid, priority) == 0
    except Exception:
        return False

def _get_rss_kb(pid: int) -> int:
    """Llegeix la RAM resident (RSS) d'un procés directament de /proc."""
    try:
        with open(f"/proc/{pid}/statm", "r") as f:
            # El segon camp és RSS en pàgines
            parts = f.read().split()
            if len(parts) < 2: return 0
            pages = int(parts[1])
            # La mida de pàgina sol ser 4096 bytes (4KB), però ho consultem al sistema
            page_size_kb = os.sysconf('SC_PAGE_SIZE') // 1024
            return pages * page_size_kb
    except (OSError, IndexError, ValueError):
        return 0

def _linear_regression(x: List[float], y: List[float]) -> Tuple[float, float]:
    """Calcula el pendent (slope) i el coeficient de determinació (R²)."""
    n = len(x)
    if n < 2: return 0.0, 0.0
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xx = sum(xi*xi for xi in x)
    sum_xy = sum(xi*yi for xi, yi in zip(x, y))
    
    numerator = (n * sum_xy) - (sum_x * sum_y)
    denominator = (n * sum_xx) - (sum_x * sum_x)
    if denominator == 0: return 0.0, 0.0
    
    slope = numerator / denominator
    
    # R² (Pearson correlation coefficient squared)
    mean_y = sum_y / n
    ss_tot = sum((yi - mean_y)**2 for yi in y)
    if ss_tot == 0: return slope, 1.0
    ss_res = sum((yi - (slope * xi + (mean_y - slope * (sum_x/n))))**2 for xi, yi in zip(x, y))
    r_squared = 1 - (ss_res / ss_tot)
    
    return slope, r_squared

def _T(ca: str, en: str) -> str:
    return en if os.environ.get('ARCHIE_LANG', 'ca') == 'en' else ca


# --------------------------------------------------------------------------- #
#  Constants d'ajust
# --------------------------------------------------------------------------- #
HISTORY_MAX = 20            # mostres a recordar (~20 min a 1/min)
PROC_HISTORY_MAX = 8        # mostres de processos (per detectar fugues de memòria)
GHOST_COOLDOWN = 30 * 60    # mínim entre dues accions fantasma noves
AUTO_THRESHOLD = 3          # accepts seguits perquè l'Archie ho faci sol
AUTO_COOLDOWN = 30 * 60     # mínim entre auto-aplicacions del mateix check
SNOOZE_BASE = 60 * 60       # 1a ignorada → silenci 1 h
SNOOZE_MAX = 7 * 24 * 3600  # sostre del backoff: 1 setmana

# Detecció de fuga de memòria: un procés que creix de forma sostinguda.
LEAK_MIN_SAMPLES = 5        # mostres mínimes per a la regressió
LEAK_MIN_RSS_KB = 200_000   # >200 MB
LEAK_SLOPE_THRESHOLD = 500  # creixement >500 KB/min (o per mostra)
LEAK_R2_THRESHOLD = 0.85    # consistència alta

# Drenatge de bateria: si baixa més ràpid que això, busquem el culpable.
DRAIN_FAST_PCT_MIN = 0.6    # %/min (≈ menys de ~2 h d'autonomia plena)

BATTERY_GLOB = "/sys/class/power_supply/BAT*"

# Apps de sincronització que es poden pausar/reprendre sense perdre res.
_SYNC_APPS = ["dropbox", "syncthing", "nextcloud", "megasync", "insync"]

# Apps que indiquen que estàs en una reunió/gravant o treballant → no interrompre (mode focus).
_FOCUS_APPS = ["obs", "zoom", ".zoom", "teams-for-linux", "skypeforlinux",
               "wf-recorder", "wlrecord", "kooha", "blue-recorder",
               "code", "vscodium", "sublime_text", "intellij", "android-studio"]


# --------------------------------------------------------------------------- #
#  Lectures barates del sistema
# --------------------------------------------------------------------------- #
def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    v = _read(path)
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _run(cmd: List[str], timeout: int = 4) -> Tuple[int, str]:
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def _run_shell(cmd: str, timeout: int = 8) -> bool:
    try:
        rc = subprocess.run(["bash", "-c", cmd], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout).returncode
        return rc == 0
    except Exception:
        return False


def _bat_dir() -> Optional[str]:
    dirs = sorted(glob.glob(BATTERY_GLOB))
    return dirs[0] if dirs else None


# --------------------------------------------------------------------------- #
#  Definició d'una acció fantasma
# --------------------------------------------------------------------------- #
@dataclass
class _Ghost:
    id: str
    want: Callable[[], bool]
    do: Callable[[], Optional[Tuple[str, str]]]
    restore_ok: Callable[[], bool]
    undo_label: str = "Desfés"
    undo_label_en: str = "Undo"
    auto_restore: bool = True
    restore_msg: str = "✓ Restaurat: tornes a estar com abans."
    restore_msg_en: str = "✓ Restored: you're back as before."

    @property
    def display_undo_label(self) -> str:
        return _T(self.undo_label, self.undo_label_en)

    @property
    def display_restore_msg(self) -> str:
        return _T(self.restore_msg, self.restore_msg_en)


# --------------------------------------------------------------------------- #
#  Cervell
# --------------------------------------------------------------------------- #
class Brain:
    def __init__(self, state) -> None:
        self.state = state
        self._ghosts: List[_Ghost] = [
            _Ghost("ghost_power_saver", self._ps_want, self._ps_do, self._charging,
                   restore_msg="🔌 Endollat: he restaurat el teu perfil d'energia.", restore_msg_en="🔌 Plugged in: restored your power profile."),
            _Ghost("ghost_tab_priority", self._tab_want, self._tab_do, self._tab_restore_ok,
                   undo_label="Restaura prioritat", undo_label_en="Restore priority",
                   restore_msg="🚀 Prioritat de pestanyes restaurada.", restore_msg_en="🚀 Tab priority restored."),
            _Ghost("ghost_bluetooth", self._bt_want, self._bt_do, self._charging,
                   restore_msg="🔌 Endollat: he tornat a encendre el Bluetooth.", restore_msg_en="🔌 Plugged in: Bluetooth turned back on."),
            _Ghost("ghost_backlight", self._bl_want, self._bl_do, self._charging,
                   restore_msg="🔌 Endollat: lluentor restaurada.", restore_msg_en="🔌 Plugged in: brightness restored."),
            _Ghost("ghost_animations", self._an_want, self._an_do, self._charging,
                   restore_msg="🔌 Endollat: animacions reactivades.", restore_msg_en="🔌 Plugged in: animations reactivated."),
            _Ghost("ghost_sync_pause", self._sync_want, self._sync_do, self._charging,
                   restore_msg="🔌 Endollat: sincronitzacions represes.", restore_msg_en="🔌 Plugged in: syncs resumed."),
        ]

    def handle_dbus_event(self, event_type: str, data: dict) -> None:
        """Processes real-time D-Bus events from dbus_listener.py."""
        if event_type == "power_status_change":
            # Immediate sample on power change to react fast
            self.sample()
        elif event_type == "system_sleep":
            self.state._save()
        elif event_type == "system_wake":
            self.sample()

    # ===================================================================== #
    #  1. MOSTREIG
    # ===================================================================== #
    def sample(self) -> dict:
        bat = _bat_dir()
        pct = _read_int(f"{bat}/capacity") if bat else None
        status = _read(f"{bat}/status") if bat else None
        load = None
        lv = _read("/proc/loadavg")
        if lv:
            try:
                load = float(lv.split()[0])
            except (ValueError, IndexError):
                pass
        s = {"t": time.time(), "pct": pct, "status": status, "load": load}
        hist = self.state.data.setdefault("history", [])
        hist.append(s)
        del hist[:-HISTORY_MAX]

        # Mostra de processos (RSS) per detectar fugues de memòria al llarg del temps.
        procs = self._top_procs()
        if procs:
            ph = self.state.data.setdefault("proc_history", [])
            ph.append({"t": s["t"], "procs": procs})
            del ph[:-PROC_HISTORY_MAX]

        self.state._save()
        return s

    def _top_procs(self, n: int = 6) -> dict:
        """{pid: [rss_kb, comm]} dels processos que més RAM ocupen."""
        res: dict = {}
        try:
            pids = [d for d in os.listdir("/proc") if d.isdigit()]
            procs = []
            for pid in pids:
                rss = _get_rss_kb(int(pid))
                if rss > 1000:
                    procs.append((pid, rss))
            
            procs.sort(key=lambda x: x[1], reverse=True)
            
            for pid, rss in procs[:n]:
                try:
                    with open(f"/proc/{pid}/comm", "r") as f:
                        comm = f.read().strip()
                    res[pid] = [rss, comm]
                except OSError: continue
        except OSError:
            pass
        return res

    def _cmdline(self, pid) -> List[str]:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return [p.decode("utf-8", "ignore") for p in f.read().split(b"\0") if p]
        except OSError:
            return []

    def _friendly_name(self, pid, comm: str) -> str:
        """Resol intèrprets (python/node/bash…) al nom de l'script real."""
        interp = ("python", "node", "nodejs", "perl", "ruby", "java", "electron",
                  "deno", "bash", "sh", "dash", "zsh")
        parts = self._cmdline(pid)
        if parts:
            first = os.path.basename(parts[0])
            if first.startswith(interp) or first in interp:
                for tok in parts[1:]:
                    if tok == "-c":
                        return comm           # codi inline: no hi ha script
                    if tok.startswith("-"):
                        continue
                    base = os.path.basename(tok)
                    if not base or base.startswith(interp) or base in interp:
                        continue
                    return base
        return comm

    def _history(self) -> List[dict]:
        return self.state.data.get("history", [])

    def on_battery(self) -> bool:
        h = self._history()
        return bool(h) and h[-1].get("status") == "Discharging"

    def _charging(self) -> bool:
        return not self.on_battery()

    def battery_pct(self) -> Optional[int]:
        for s in reversed(self._history()):
            if s.get("pct") is not None:
                return s["pct"]
        return None

    def is_idle(self) -> bool:
        """Repòs REAL: diverses mostres seguides amb càrrega baixa.

        Mirar només la instantània enganya (entre frames d'un render el load
        baixa un instant). Exigim ≥3 mostres i que CAP passi d'1.2.
        """
        loads = [s["load"] for s in self._history()[-5:] if s.get("load") is not None]
        return len(loads) >= 3 and max(loads) < 1.2

    # ===================================================================== #
    #  2. APRENENTATGE
    # ===================================================================== #
    def _fb(self, cid: str) -> dict:
        return self.state.data.setdefault("feedback", {}).setdefault(cid, {})

    def record_feedback(self, cid: str, accepted: bool) -> None:
        fb = self._fb(cid)
        if accepted:
            fb["accept"] = fb.get("accept", 0) + 1
            fb["streak_accept"] = fb.get("streak_accept", 0) + 1
            fb["streak_dismiss"] = 0
        else:
            # Si no hi ha fix possible, no ho comptem com a "ignorat" negativament
            # (és només un avís informatiu).
            import monitor
            check = next((c for c in monitor.get_checks() if c.id == cid), None)
            if check and not check.fix:
                return

            fb["dismiss"] = fb.get("dismiss", 0) + 1
            fb["streak_dismiss"] = fb.get("streak_dismiss", 0) + 1
            fb["streak_accept"] = 0
            # Si rebutja una automatització que ja havia après, para de fer-la sol.
            if fb.get("last_auto"):
                fb["auto_disabled"] = True
        fb["last"] = time.time()
        fb["last_kind"] = "accept" if accepted else "dismiss"
        self.state._save()

    def snooze_for(self, cid: str) -> int:
        """Backoff: com més ignores un tema, més estona calla (fins 1 setmana)."""
        streak = self._fb(cid).get("streak_dismiss", 1) or 1
        return int(min(SNOOZE_BASE * (2 ** (streak - 1)), SNOOZE_MAX))

    def should_autoapply(self, check) -> bool:
        """L'Archie ho fa sol si: el check és marcat 'auto', té 'undo', l'has
        acceptat prou cops seguits i no fa poc que ho va fer."""
        if not getattr(check, "auto", False) or not getattr(check, "undo", None):
            return False
        fb = self._fb(check.id)
        if fb.get("auto_disabled"):
            return False
        if fb.get("streak_accept", 0) < AUTO_THRESHOLD:
            return False
        if time.time() - fb.get("last_auto", 0) < AUTO_COOLDOWN:
            return False
        return True

    def mark_autoapplied(self, check) -> None:
        self._fb(check.id)["last_auto"] = time.time()
        active = self.state.data.setdefault("ghosts", {})
        active[check.id] = {
            "undo": check.undo, "msg": check.display_message,
            "applied_at": time.time(), "auto_restore": False, "learned": True,
        }
        self.state._save()

    # ===================================================================== #
    #  3. ACCIONS FANTASMA
    # ===================================================================== #
    def tick(self) -> Optional[dict]:
        """Auto-restaura accions caducades i, si toca, aplica'n una de nova.

        Retorna un esdeveniment per ensenyar (apply o restore) o None.
        """
        active = self.state.data.setdefault("ghosts", {})
        defs = {g.id: g for g in self._ghosts}

        # 3a. Desfer soles les que ja no tenen sentit (p. ex. has endollat).
        for gid, info in list(active.items()):
            if not info.get("auto_restore"):
                continue
            g = defs.get(gid)
            if g and g.restore_ok():
                _run_shell(info["undo"])
                active.pop(gid, None)
                self.state.data["last_ghost_time"] = time.time()
                self.state._save()
                return {"id": gid, "kind": "restore",
                        "message": info.get("restore_msg") or g.display_restore_msg,
                        "undo": None}

        # 3b. Aplicar-ne una de nova (amb cooldown global per no atabalar).
        if time.time() - self.state.data.get("last_ghost_time", 0) < GHOST_COOLDOWN:
            return None
        for g in self._ghosts:
            if g.id in active:
                continue
            try:
                if not g.want():
                    continue
                res = g.do()
            except Exception:
                res = None
            if not res:
                continue
            undo, msg = res
            active[g.id] = {"undo": undo, "msg": msg, "applied_at": time.time(),
                            "auto_restore": g.auto_restore,
                            "restore_msg": g.display_restore_msg}
            self.state.data["last_ghost_time"] = time.time()
            self.state._save()
            return {"id": g.id, "kind": "apply", "message": msg,
                    "undo": undo, "undo_label": g.display_undo_label}
        return None

    def clear_ghost(self, gid: str) -> None:
        active = self.state.data.get("ghosts", {})
        if gid in active:
            active.pop(gid, None)
            self.state._save()

    def undo_all(self) -> List[Tuple[str, str]]:
        active = self.state.data.get("ghosts", {})
        done: List[Tuple[str, str]] = []
        for gid, info in list(active.items()):
            if info.get("undo"):
                _run_shell(info["undo"])
            done.append((gid, info.get("msg", "")))
        self.state.data["ghosts"] = {}
        self.state._save()
        return done

    # ---- helpers d'estat de maquinari ---------------------------------- #
    def _profile(self) -> Optional[str]:
        rc, out = _run(["powerprofilesctl", "get"])
        return out if rc == 0 and out else None

    def _bctl(self, what: str) -> Optional[int]:
        rc, out = _run(["brightnessctl", what])
        try:
            return int(out)
        except ValueError:
            return None

    # ---- ghost: perfil d'energia --------------------------------------- #
    def _ps_want(self) -> bool:
        return bool(self.on_battery() and (self.battery_pct() or 100) <= 50
                    and self.is_idle() and shutil.which("powerprofilesctl")
                    and self._profile() not in (None, "power-saver"))

    def _ps_do(self) -> Optional[Tuple[str, str]]:
        prof = self._profile()
        if not prof:
            return None
        rc, _ = _run(["powerprofilesctl", "set", "power-saver"])
        if rc != 0:
            return None
        return (f"powerprofilesctl set {prof}",
                _T("👻 Mode estalvi activat: bateria baixa i equip en repòs.", "👻 Power saving mode on: low battery and idle."))

    # ---- ghost: prioritat de pestanyes Brave/Chrome --------------------- #
    def _tab_want(self) -> bool:
        # Si estem amb bateria i el PC entra en repòs, baixem la prioritat de les pestanyes.
        if not (self.on_battery() and self.is_idle()):
            return False
        # Comprovem si hi ha Brave corrent (llegint /proc)
        try:
            for d in os.listdir("/proc"):
                if not d.isdigit(): continue
                try:
                    with open(f"/proc/{d}/cmdline", "r") as f:
                        cmd = f.read()
                        if "brave" in cmd and "--type=renderer" in cmd:
                            return True
                except OSError: continue
        except OSError: pass
        return False

    def _tab_do(self) -> Optional[Tuple[str, str]]:
        # Posem les pestanyes a la prioritat més baixa (19) via libc
        pids = []
        try:
            for d in os.listdir("/proc"):
                if not d.isdigit(): continue
                try:
                    with open(f"/proc/{d}/cmdline", "r") as f:
                        cmd = f.read()
                        if "brave" in cmd and "--type=renderer" in cmd:
                            pid = int(d)
                            if _set_priority(pid, 19):
                                pids.append(pid)
                except OSError: continue
        except OSError: pass
        
        if not pids: return None

        # Robust undo: use the libc wrapper via python inline instead of renice
        undo_cmd = f"python3 -c 'import ctypes, ctypes.util; libc = ctypes.CDLL(ctypes.util.find_library(\"c\")); [libc.setpriority(0, p, 0) for p in {pids}]'"

        return (undo_cmd,
                _T("👻 Autopilot: he baixat la prioritat de les pestanyes per estalviar bateria.",
                   "👻 Autopilot: lowered tab priority to save battery."))

    def _tab_restore_ok(self) -> bool:
        # Restaurem si s'endolla O si l'ordinador deja d'estar "idle"
        return self._charging() or not self.is_idle()

    # ---- ghost: bluetooth ---------------------------------------------- #
    def _bt_want(self) -> bool:
        if not (self.on_battery() and shutil.which("rfkill")):
            return False
        rc, out = _run(["rfkill", "list", "bluetooth"])
        if rc != 0 or "Soft blocked" not in out or "Soft blocked: yes" in out:
            return False  # sense BT o ja apagat
        if shutil.which("bluetoothctl"):
            rc2, dev = _run(["bluetoothctl", "devices", "Connected"])
            if rc2 != 0:
                return False           # no podem assegurar que no hi hagi res
            if dev.strip():
                return False           # tens algun dispositiu connectat
        return True

    def _bt_do(self) -> Optional[Tuple[str, str]]:
        if not _run(["rfkill", "block", "bluetooth"])[0] == 0:
            return None
        return ("rfkill unblock bluetooth",
                _T("👻 Bluetooth apagat: no l'estaves usant i drena bateria.", "👻 Bluetooth off: not in use, saves battery."))

    # ---- ghost: lluentor ----------------------------------------------- #
    def _bl_want(self) -> bool:
        if not (self.on_battery() and (self.battery_pct() or 100) <= 40
                and shutil.which("brightnessctl")):
            return False
        cur, mx = self._bctl("get"), self._bctl("max")
        return bool(cur and mx and (cur / mx) > 0.7)

    def _bl_do(self) -> Optional[Tuple[str, str]]:
        cur = self._bctl("get")
        if not cur:
            return None
        rc, _ = _run(["brightnessctl", "-q", "set", "50%"])
        if rc != 0:
            return None
        return (f"brightnessctl set {cur}",
                _T("👻 Pantalla atenuada al 50%: estalvi amb bateria baixa.", "👻 Screen dimmed to 50%: saving battery."))

    # ---- ghost: animacions Hyprland ------------------------------------ #
    def _an_want(self) -> bool:
        if not (self.on_battery() and (self.battery_pct() or 100) <= 25
                and shutil.which("hyprctl")):
            return False
        rc, out = _run(["hyprctl", "getoption", "animations:enabled"])
        return rc == 0 and "int: 1" in out

    def _an_do(self) -> Optional[Tuple[str, str]]:
        rc, _ = _run(["hyprctl", "keyword", "animations:enabled", "0"])
        if rc != 0:
            return None
        return ("hyprctl keyword animations:enabled 1",
                _T("👻 Animacions desactivades: bateria crítica, a estirar-la.", "👻 Animations disabled: critical battery, stretching it out."))

    # ===================================================================== #
    #  ANÀLISI: anomalies que només es veuen amb tendències
    # ===================================================================== #
    def leak_suspect(self) -> Optional[dict]:
        """Detecta fuga de memòria via Regressió Lineal."""
        ph = self.state.data.get("proc_history", [])
        if len(ph) < LEAK_MIN_SAMPLES:
            return None
        
        # Obtenim tots els PIDs presents a l'historial recent
        window = ph[-LEAK_MIN_SAMPLES:]
        all_pids = set()
        for s in window:
            all_pids.update(s["procs"].keys())
            
        best_leak = None
        
        for pid in all_pids:
            # Construïm la sèrie de dades per a aquest PID
            y = []
            x = []
            comm = "desconegut"
            for i, s in enumerate(window):
                if pid in s["procs"]:
                    y.append(float(s["procs"][pid][0]))
                    x.append(float(i))
                    comm = s["procs"][pid][1]
            
            if len(y) < LEAK_MIN_SAMPLES: continue
            
            slope, r2 = _linear_regression(x, y)
            
            if y[-1] > LEAK_MIN_RSS_KB and slope > LEAK_SLOPE_THRESHOLD and r2 > LEAK_R2_THRESHOLD:
                if best_leak is None or slope > best_leak[0]:
                    best_leak = (slope, pid, y[0], y[-1], comm, r2)
        
        if not best_leak:
            return None
            
        slope, pid, first, last, comm, r2 = best_leak
        name = self._friendly_name(pid, comm)
        return {
            "id": f"leak_{name}",
            "category": "memory",
            "message": _T(f"'{name}' sembla tenir una fuga de memòria (creix consistentment, R²={r2:.2f}). Vols reiniciar-lo?",
                          f"'{name}' seems to have a memory leak (consistent growth, R²={r2:.2f}). Restart it?"),
            "fix": f"kill {pid}",
            "label": _T("Reinicia'l", "Restart it"),
        }

    def _drain_rate(self) -> Optional[float]:
        """%/min que baixa la bateria, sobre la finestra recent (si descarrega)."""
        pts = [(s["t"], s["pct"]) for s in self._history()[-10:]
               if s.get("pct") is not None]
        if len(pts) < 3:
            return None
        dt = (pts[-1][0] - pts[0][0]) / 60.0
        dp = pts[0][1] - pts[-1][1]   # positiu si baixa
        if dt < 3 or dp <= 0:
            return None
        return dp / dt

    def battery_eta(self) -> Optional[int]:
        """Minuts estimats que queden de bateria al ritme actual."""
        if not self.on_battery():
            return None
        rate, pct = self._drain_rate(), self.battery_pct()
        if not rate or not pct:
            return None
        return int(pct / rate)

    def drain_suspect(self) -> Optional[dict]:
        """Avisa (i assenyala el culpable) si la bateria es buida molt ràpid."""
        if not self.on_battery():
            return None
        rate = self._drain_rate()
        if not rate or rate < DRAIN_FAST_PCT_MIN:
            return None
        
        # Culpable via /proc
        name = "alguna cosa"
        try:
            # Això és més car, però només ho fem si hi ha drain fast
            # Per simplicitat usem el que ja tenim de _top_procs però per CPU seria millor
            rc, out = _run(["ps", "-eo", "pid=,comm=", "--sort=-pcpu"])
            if rc == 0 and out:
                top = out.splitlines()[0].split(None, 1)
                if len(top) == 2:
                    name = self._friendly_name(top[0].strip(), top[1].strip())
        except: pass

        eta = self.battery_eta()
        eta_txt = f"~{eta} min" if eta else "poca estona"
        return {
            "id": "fast_drain",
            "category": "battery",
            "message": _T(f"La bateria baixa molt ràpid ({eta_txt} al ritme actual). El que més consumeix ara és '{name}'.",
                          f"Battery draining very fast ({eta_txt} at current rate). Top consumer right now is '{name}'."),
            "fix": None,
            "label": _T("Entesos", "Got it"),
        }

    def in_focus_mode(self) -> bool:
        """Estàs gravant/en reunió o amb una finestra a pantalla completa?"""
        try:
            for d in os.listdir("/proc"):
                if not d.isdigit(): continue
                try:
                    with open(f"/proc/{d}/comm", "r") as f:
                        comm = f.read().strip()
                        if any(app in comm for app in _FOCUS_APPS):
                            return True
                except OSError: continue
        except OSError: pass

        if shutil.which("hyprctl"):
            rc, o = _run(["hyprctl", "activewindow", "-j"])
            if rc == 0 and ('"fullscreen": 1' in o or '"fullscreen": 2' in o
                            or '"fullscreen": true' in o):
                return True
        return False

    # ---- ghost: pausar sincronitzacions -------------------------------- #
    def _pgrep_pids(self, name: str) -> List[int]:
        """Versió nativa de pgrep que retorna PIDs llegint de /proc."""
        pids = []
        try:
            for d in os.listdir("/proc"):
                if not d.isdigit(): continue
                try:
                    with open(f"/proc/{d}/comm", "r") as f:
                        if f.read().strip() == name:
                            pids.append(int(d))
                except OSError: continue
        except OSError: pass
        return pids

    def _sync_want(self) -> bool:
        if not (self.on_battery() and (self.battery_pct() or 100) <= 30):
            return False
        return any(bool(self._pgrep_pids(a)) for a in _SYNC_APPS)

    def _sync_do(self) -> Optional[Tuple[str, str]]:
        paused = []
        all_pids = []
        for a in _SYNC_APPS:
            pids = self._pgrep_pids(a)
            if pids:
                for p in pids:
                    try:
                        os.kill(p, signal.SIGSTOP)
                        all_pids.append(p)
                    except OSError: continue
                paused.append(a)
        if not paused:
            return None
        
        # Undo robust via os.kill en python inline
        undo = f"python3 -c 'import os, signal; [os.kill(p, signal.SIGCONT) for p in {all_pids}]'"
        
        return (undo, _T(f"👻 Sincronitzacions en pausa ({', '.join(paused)}): estalvi amb bateria baixa.",
                     f"👻 Syncs paused ({', '.join(paused)}): saving battery."))

    # ===================================================================== #
    #  Transparència ("què saps de mi?")
    # ===================================================================== #
    def insights(self) -> dict:
        h = self._history()
        last = h[-1] if h else {}
        loads = [s.get("load") for s in h[-5:] if s.get("load") is not None]

        ghosts = [
            {"id": gid, "msg": info.get("msg", ""),
             "learned": info.get("learned", False),
             "auto_restore": info.get("auto_restore", False)}
            for gid, info in self.state.data.get("ghosts", {}).items()
        ]

        try:
            auto_ids = {c.id for c in monitor.get_checks() if getattr(c, "auto", False)}
        except Exception:
            auto_ids = set()

        habits = []
        for cid, fb in self.state.data.get("feedback", {}).items():
            habits.append({
                "id": cid,
                "accept": fb.get("accept", 0),
                "dismiss": fb.get("dismiss", 0),
                "streak_accept": fb.get("streak_accept", 0),
                "streak_dismiss": fb.get("streak_dismiss", 0),
                "auto": cid in auto_ids,
                "auto_disabled": fb.get("auto_disabled", False),
                "last_kind": fb.get("last_kind", ""),
            })
        habits.sort(key=lambda x: -(x["accept"] + x["dismiss"]))

        return {
            "battery": {"pct": last.get("pct"), "status": last.get("status"),
                        "on_battery": self.on_battery(), "eta_min": self.battery_eta()},
            "idle": self.is_idle(),
            "focus": self.in_focus_mode(),
            "loads": loads,
            "samples": len(h),
            "ghosts": ghosts,
            "habits": habits,
        }
