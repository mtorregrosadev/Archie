DBUS_ACTIVE = False
"""
monitor.py — Motor de checks d'Archie, dirigit per dades (archie_checks.yaml).

Cada check és una comanda de shell `detect` (codi de sortida 0 => alerta) i una
comanda `fix` (o null si és informatiu). El motor:

  * omet els checks la primera eina dels quals no està instal·lada,
  * posa un timeout a cada detecció perquè res no es pengi,
  * cacheja el resultat segons la categoria (els checks cars no es repeteixen
    cada cicle),
  * avalua per prioritat i pot parar al primer que salta (per al popup).

No depèn de GTK. Prova'l pel seu compte:

    /usr/bin/python3 monitor.py            # avalua-ho tot i ensenya l'estat
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# PyYAML si hi és; si no, un mini-parser per al nostre subconjunt.
try:
    import yaml  # type: ignore
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

CHECKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "archie_checks.yaml")

DETECT_TIMEOUT = 8  # segons màxim per comanda de detecció

# Helpers de shell injectats abans de CADA detect i fix. Resolen "qui és el procés
# culpable" amb un nom llegible —els intèrprets (python/node/bash…) es resolen a
# l'script real, no a "/usr/sbin/python"— i permeten matar PER PID (segur), no per
# nom (que podria matar processos que no toca, fins i tot l'Archie).
_PREAMBLE = r"""
archie_top_pid() {  # $1 = pcpu (defecte) o pmem
  ps -eo pid=,comm= --sort=-${1:-pcpu} 2>/dev/null | while read -r _p _c; do
    case "$_c" in
      ps|awk|grep|bash|sh|sort|head|cat|sed|tr|cut|paste|wc) continue;;
    esac
    echo "$_p"; break
  done
}
archie_proc_label() {  # $1 = PID -> nom llegible
  [ -z "$1" ] && return
  _comm=$(ps -p "$1" -o comm= 2>/dev/null); _comm=${_comm##*/}
  case "$_comm" in
    python*|node|nodejs|bash|sh|dash|zsh|perl|ruby|java|electron|deno)
      for _t in $(ps -p "$1" -o args= 2>/dev/null); do
        case "$_t" in
          -c) printf '%s' "$_comm"; return;;   # codi inline: no hi ha script
          -*) continue;;
          *python*|node|nodejs|*/node|bash|*/bash|sh|*/sh|perl|ruby|java|electron|*/electron) continue;;
          *) _t=${_t##*/}; printf '%s' "$_t"; return;;
        esac
      done;;
  esac
  printf '%s' "$_comm"
}
"""

# Estats possibles d'un check.
ALERT = "alert"      # detect ha sortit amb 0 → hi ha cosa a dir
OK = "ok"            # tot correcte
SKIP = "skip"        # eina no instal·lada → no es pot comprovar
ERROR = "error"      # timeout o error inesperat

# Prioritat per categoria (més alt = es mostra abans al popup).
CATEGORY_PRIORITY: Dict[str, int] = {
    "thermals": 100,
    "battery": 90,
    "memory": 85,
    "cpu": 80,
    "security": 70,
    "ssd": 50,
    "gpu": 45,
    "network": 40,
    "performance": 38,
    "wayland": 30,
    "boot": 20,
}

# Cada quant té sentit re-executar la detecció (segons). 0 = sempre fresc.
CATEGORY_TTL: Dict[str, int] = {
    "thermals": 0,
    "memory": 0,
    "cpu": 0,
    "performance": 0,
    "battery": 1800,
    "wayland": 900,
    "ssd": 3600,
    "gpu": 3600,
    "network": 3600,
    "boot": 3600,
    "security": 3600,
}
TTL_OVERRIDE: Dict[str, int] = {
    "system_not_updated": 6 * 3600,   # checkupdates és lent i toca la xarxa
    "dns_slow": 3600,
    "boot_slow": 24 * 3600,           # només canvia en reiniciar
    "battery_health_low": 6 * 3600,
}

# Ordre de categories per a la vista d'estat.
CATEGORY_ORDER = [
    "thermals", "cpu", "memory", "ssd", "wayland",
    "boot", "battery", "security", "gpu", "network", "performance",
]


# --------------------------------------------------------------------------- #
#  Model
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    id: str
    category: str
    message: str
    detect: str
    fix: Optional[str] = None
    label: str = "Arregla-ho"
    message_en: str = ""
    label_en: str = "Fix it"
    once: bool = False
    critical: bool = False
    undo: Optional[str] = None   # com revertir el fix (per a auto-aplicació i ghost)
    auto: bool = False           # si true, l'Archie el pot acabar fent sol si és segur
    order: int = 0  # posició dins el fitxer (desempat de prioritat)

    _status: str = field(default="unknown", repr=False)
    _last_run: float = field(default=0.0, repr=False)
    _available: Optional[bool] = field(default=None, repr=False)
    _out: str = field(default="", repr=False)  # stdout del detect, per renderir {}

    @property
    def priority(self) -> int:
        return CATEGORY_PRIORITY.get(self.category, 10)

    @property
    def ttl(self) -> int:
        if self.id in TTL_OVERRIDE:
            return TTL_OVERRIDE[self.id]
        return CATEGORY_TTL.get(self.category, 600)

    def _primary_tool(self) -> str:
        """El primer binari de la canonada de detecció (per saber si hi és)."""
        s = self.detect.strip()
        while s and s[0] in "!({":
            s = s[1:].strip()
        first = s.split()[0] if s.split() else ""
        # Una assignació tipus `f=/sys/...` no és un binari.
        if "=" in first and "/" not in first.split("=")[0]:
            return ""  # comença amb VAR=...; no podem saber l'eina, assumeix-la
        return first

    def available(self) -> bool:
        if self._available is None:
            tool = self._primary_tool()
            self._available = (tool == "") or (shutil.which(tool) is not None)
        return self._available

    def evaluate(self, force: bool = False) -> str:
        """Retorna ALERT / OK / SKIP / ERROR, fent servir la cache si toca."""
        if not force and self._status != "unknown":
            effective_ttl = self.ttl
            if self.category == "battery" and DBUS_ACTIVE:
                effective_ttl = max(effective_ttl, 3600)  # Poll once per hour if DBus is live
            if (time.time() - self._last_run) < effective_ttl:
                return self._status

        if not self.available():
            self._status, self._last_run = SKIP, time.time()
            return self._status

        try:
            result = subprocess.run(
                ["bash", "-c", _PREAMBLE + "\n" + self.detect],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=DETECT_TIMEOUT,
                text=True,
            )
            rc = result.returncode
            if rc == 0:
                self._status = ALERT
                # Desa la sortida per renderir {} EN CALENT, sense mutar el
                # template (els Check es reutilitzen entre cicles → mutar-los
                # feia que un cop substituït es quedés enganxat per sempre).
                self._out = result.stdout.strip()
            else:
                self._status = OK
        except subprocess.TimeoutExpired:
            self._status = ERROR
        except Exception:
            self._status = ERROR
        self._last_run = time.time()
        return self._status

    @property
    def display_message(self) -> str:
        lang = os.environ.get("ARCHIE_LANG", "ca")
        base = self.message_en if lang == "en" and self.message_en else self.message
        if self._out and "{}" in base:
            return base.replace("{}", self._out)
        return base

    @property
    def display_label(self) -> str:
        lang = os.environ.get("ARCHIE_LANG", "ca")
        return self.label_en if lang == "en" and self.label_en else self.label

    @property
    def run_command(self) -> Optional[str]:
        """El fix amb {} substituït per la sortida del detect (si escau)."""
        if self.fix and self._out and "{}" in self.fix:
            return self.fix.replace("{}", self._out)
        return self.fix


# --------------------------------------------------------------------------- #
#  Càrrega del YAML
# --------------------------------------------------------------------------- #
def _mini_yaml_load(text: str) -> List[dict]:
    """Parser mínim per al subconjunt d'archie_checks.yaml (sense PyYAML).

    Suporta: clau: valor, on valor és "cadena entre cometes", null, true/false
    o una paraula. Tracta les llistes amb '- clau: valor'.
    """
    items: List[dict] = []
    current: Optional[dict] = None
    in_checks = False

    def parse_scalar(raw: str):
        raw = raw.strip()
        if not raw:
            return ""
        if raw[0] == '"':
            end = raw.find('"', 1)
            val = raw[1:end] if end != -1 else raw[1:]
            return val.replace("\\\\", "\\")  # \\ -> \
        # treu comentari final fora de cometes
        hashpos = raw.find(" #")
        if hashpos != -1:
            raw = raw[:hashpos].strip()
        if raw == "null":
            return None
        if raw == "true":
            return True
        if raw == "false":
            return False
        return raw

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not in_checks:
            if stripped.startswith("checks:"):
                in_checks = True
            continue
        if stripped.startswith("- "):
            if current is not None:
                items.append(current)
            current = {}
            stripped = stripped[2:].strip()  # resta: primera clau de l'ítem
        if current is None:
            continue
        if ":" not in stripped:
            continue
        key, _, raw = stripped.partition(":")
        current[key.strip()] = parse_scalar(raw)

    if current is not None:
        items.append(current)
    return items


def load_checks(path: str = CHECKS_FILE) -> List[Check]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if _HAVE_YAML:
        data = yaml.safe_load(text) or {}
        raw_checks = data.get("checks", [])
    else:
        raw_checks = _mini_yaml_load(text)

    checks: List[Check] = []
    for i, c in enumerate(raw_checks):
        if not c.get("id") or not c.get("detect"):
            continue
        checks.append(Check(
            id=c["id"],
            category=c.get("category", "misc"),
            message=c.get("message", ""),
            message_en=c.get("message_en", ""),
            detect=c["detect"],
            fix=c.get("fix") or None,
            label=c.get("label") or "Arregla-ho",
            label_en=c.get("label_en") or "Fix it",
            once=bool(c.get("once", False)),
            critical=bool(c.get("critical", False)),
            undo=c.get("undo") or None,
            auto=bool(c.get("auto", False)),
            order=i,
        ))
    return checks


# Es carrega un sol cop; conserva la cache entre cicles.
_CHECKS: Optional[List[Check]] = None


def get_checks(reload: bool = False) -> List[Check]:
    global _CHECKS
    if _CHECKS is None or reload:
        _CHECKS = load_checks()
    return _CHECKS


# --------------------------------------------------------------------------- #
#  API d'avaluació
# --------------------------------------------------------------------------- #
def _by_priority(checks: List[Check]) -> List[Check]:
    return sorted(checks, key=lambda c: (-c.priority, c.order))


def first_alert(is_blocked: Callable[[str], bool] = lambda _i: False,
                force: bool = False,
                only_critical: bool = False) -> Optional[Check]:
    """Avalua per prioritat i retorna el PRIMER check en alerta (no bloquejat).

    Para tan aviat com en troba un → no cal avaluar la resta (eficient).
    """
    for check in _by_priority(get_checks()):
        if only_critical and not check.critical:
            continue
        if is_blocked(check.id):
            continue
        if check.evaluate(force=force) == ALERT:
            return check
    return None


def evaluate_all(force: bool = True) -> List[Tuple[Check, str]]:
    """Avalua TOTS els checks (per a la vista d'estat)."""
    return [(c, c.evaluate(force=force)) for c in get_checks()]


# --------------------------------------------------------------------------- #
#  Execució de fixos (en un terminal, per veure el resultat i poder fer sudo)
# --------------------------------------------------------------------------- #
_TERMINALS = [
    ("ghostty", ["ghostty", "-e"]),
    ("kitty", ["kitty"]),
    ("alacritty", ["alacritty", "-e"]),
    ("foot", ["foot"]),
    ("wezterm", ["wezterm", "start", "--"]),
    ("konsole", ["konsole", "-e"]),
    ("gnome-terminal", ["gnome-terminal", "--"]),
    ("xterm", ["xterm", "-e"]),
]


def _find_terminal() -> Optional[List[str]]:
    for name, argv in _TERMINALS:
        if shutil.which(name):
            return argv
    return None


def run_fix(fix_command: str, silent: bool = False) -> bool:
    """Executa el fix dins un terminal que es queda obert amb el resultat.

    Si silent=True, l'executa directament pel darrere sense obrir finestres
    i retorna l'èxit o fracàs silenciosament (ideal per a 'archie fix').
    """
    if silent:
        try:
            rc = subprocess.run(
                ["bash", "-c", _PREAMBLE + "\n" + fix_command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            return rc == 0
        except Exception:
            return False

    term = _find_terminal()
    # Si va bé: avisa i tanca sol als 2 s (no et fa prémer res).
    # Si falla: deixa la finestra oberta perquè puguis llegir l'error.
    wrapper = (
        f"{fix_command}\n"
        "rc=$?\n"
        "echo\n"
        'if [ $rc -eq 0 ]; then\n'
        '  echo "✓ Fet. (aquesta finestra es tanca sola…)"\n'
        '  sleep 2\n'
        'else\n'
        '  echo "✗ Ha fallat (codi $rc)."\n'
        '  echo "[ Prem Enter per tancar ]"\n'
        '  read _\n'
        'fi\n'
    )
    wrapper = _PREAMBLE + "\n" + wrapper
    try:
        if term is not None:
            subprocess.Popen(term + ["bash", "-lc", wrapper],
                             start_new_session=True)
        else:
            # Sense terminal: l'executem desacoblat
            subprocess.Popen(["bash", "-lc", fix_command],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Petita demo per consola
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    print(f"YAML backend: {'PyYAML' if _HAVE_YAML else 'mini-parser'}")
    checks = get_checks()
    print(f"Checks carregats: {len(checks)}\n")
    t0 = time.time()
    results = evaluate_all(force=True)
    dt = time.time() - t0
    sym = {ALERT: "✗", OK: "✓", SKIP: "·", ERROR: "!"}
    for c, st in sorted(results, key=lambda x: (-x[0].priority, x[0].order)):
        line = f"  {sym[st]} [{st:5}] {c.id}"
        if st == ALERT:
            line += f"  → {c.display_message}"
        print(line)
    n_alert = sum(1 for _, s in results if s == ALERT)
    print(f"\n{n_alert} alertes en {dt:.2f}s")
