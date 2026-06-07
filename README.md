# 🐱 Archie

A tiny floating pet for **Hyprland / Wayland** that monitors the system
and, only when necessary, appears in the bottom right corner with an optimization suggestion
and two buttons: **Fix it** and **Not now**.

The rest of the time it is invisible. It never bothers you more than once every 15 minutes.

```
 /\_/\
( o.o )   "Swappiness too high for SSD (>10). Reduce it to 10?"
 > ^ <      [ Fix it ]  [ Not now ]
```

![Archie Example](assets/archie_example.png)

## Commands

```bash
archie            # (no args) starts the daemon — usually via systemd
archie status     # console table: ✓ OK / ✗ ALERT / · skipped, for ALL checks
archie brain      # transparency: what Archie has sampled and learned about you
archie undo       # undoes all the automations Archie has applied
archie reset [all]# clears snoozes (with 'all', also learning and history)
archie check      # checks right now and shows it as a bubble (or "all OK")
archie config     # interactive menu (with arrows) to configure options
archie demo       # example bubble that does NOT disappear on its own (for testing)
archie help
```

## Architecture and How it Works

Below is an ASCII diagram showing how Archie's different parts interact:

```text
                  +--------------------------------+
                  |         System (OS)            |
                  +--------------------------------+
                                 ^
                                 | (Sensors, CPU, RAM, processes)
  +------------------+           v             +------------------+
  | dbus_listener.py | <--- (Metrics) -------> |    monitor.py    |
  |  (Real-time      |           |             | (Checks Engine)  |
  |  Events/Wake)    |           |             |  Reads the YAML  |
  +------------------+           |             +------------------+
          |                      v                     |
  (Hardware Signals)     +------------------+          |
          |------------> |    brain.py      |          |
                         |  (Intelligence,  |          |
                         |  Habits, Ghosts) |          |
                         +------------------+          |
                                 ^                     |
                                 | (Feedback)          | (Alerts)
                                 v                     v
  +-------------------------------------------------------------+
  |                         archie.py                           |
  |                     (Application / CLI)                     |
  +-------------------------------------------------------------+
          |                                            |
   (Daemon Mode)                                 (CLI Commands)
          |                                            |
          v                                            v
  +------------------+                         +------------------+
  |  GTK4 Layer UI   |                         | Terminal Output  |
  |  (Visual Pet)    |                         | (status, brain,  |
  +------------------+                         |  fix, config...) |
```

## Behavior and Triggers

Archie operates using a hybrid engine (Event-driven + Periodic Polling):

1. **Real-time Triggers (D-Bus):** `dbus_listener.py` is constantly listening in the background at 0% CPU. When you **plug/unplug your laptop** or **wake it from sleep**, the D-Bus fires an event. Archie's `brain.py` reacts in milliseconds, immediately applying ghost optimizations (like dimming the screen or turning off Bluetooth) and popping up a bubble.
2. **Periodic Polling (YAML):** For things that don't emit hardware events (like RAM filling up, CPU overheating, or a rogue background process), the `monitor.py` engine still relies on `archie_checks.yaml`. However, it now does this incredibly efficiently: if the D-Bus listener is active, Archie only polls the battery every 1 hour, trusting the hardware to wake it up if needed.
3. **Adaptive silence**: when clicking "Not now", the topic remains silent with **exponential backoff**
  (1 h → 2 h → 4 h… up to 1 week). The more you ignore it, the less it bothers you.
4. **State** in `~/.local/state/archie/state.json`: last warning, silenced topics,
  applied optimizations (`once`), **metrics history**, **learning** and
  **active automations**.

## The Brain (`brain.py`) — the "intelligent" part

Archie doesn't just look at a snapshot: **it samples trends, learns from you and
acts on its own** on safe and reversible things.

- **Trend sampling**: every cycle saves battery, load and RAM. To decide
  if you are *really* idle it looks at **several consecutive samples** (not just the instant, which
  is deceiving between render frames).
- **Learning** (`archie brain`): remembers what you accept and what you ignore. The more
  you ignore → more silence (backoff). The more you accept a **safe and reversible** fix
  (marked `auto: true`) → when you reach **3 in a row**, Archie **does it alone** and
  only shows you an **«Undo»**. If you undo it, it stops doing it automatically.
- **Ghost actions** (contextual and reversible): when you are on **battery** and
  the equipment is idle, it can — only if you have the tools — set the **power-saving
  profile**, **turn off Bluetooth** that you don't use, **dim the screen** or
  **disable Hyprland animations**. **They undo themselves when you plug** the
  charger in. Everything is listed in `archie brain` and revertible with `archie undo`.

## The checks live in the YAML

Everything Archie checks is in **`archie_checks.yaml`** (editable):

```yaml
- id: cpu_governor_wrong
  category: cpu
  message: "CPU in extreme mode. Set it to 'balanced'?"
  detect: "...scaling_governor | grep -qv 'balanced\\|power-saver'"  # exit code 0 => ALERT
  fix:  "powerprofilesctl set balanced"     # or null if it's informative
  undo: "powerprofilesctl set performance"  # how to revert it
  auto: true                                # Archie can end up doing it alone
  # other optionals: label: "Show it"   once: true   critical: true
```

If the `message` or the `fix` contain `{}`, it injects the **output of the `detect`**
(e.g., the name of the process burning CPU) without damaging the template.

There are ~40 checks: thermals, CPU/power, RAM, SSD, Wayland-vs-X11, boot,
battery, security, GPU, network and performance. `archie status` shows them all.

**If a `detect` tool is not installed, the check is SKIPPED** (it does not fail);
`archie status` marks it with `·`.

### How fixes are applied

When clicking **Fix it**, the `fix` runs **inside a terminal that stays open**
with the result. This way you can type the password (`sudo`) and see if it went well.
The `fix: null` are just informative ("Understood" button).

## Installation

```bash
./install.sh
```

This: installs dependencies with `pacman` (`gtk4`, `gtk4-layer-shell`,
`python-gobject`, `python-yaml`), copies the app and the YAML to `~/.local/share/archie/`,
places the `archie` command in `~/.local/bin/`, and activates the user service
`~/.config/systemd/user/archie.service`.

Optional detection tools (lm_sensors, pacman-contrib, bind, mesa-utils…)
and `mbpfan` (AUR) are recommended but not forced. A terminal is required (alacritty,
kitty, foot…) so fixes with `sudo` can prompt for a password.

```bash
systemctl --user status archie.service        # service status
journalctl --user -u archie.service -f        # logs
systemctl --user disable --now archie.service # uninstall the service
```

## Performance (how it is optimized)

- The periodic sweep runs in a **background thread** → the UI never blocks.
- **Caching per check** by category: expensive ones (`checkupdates`, `dig`, boot…)
  don't repeat every cycle; real-time ones (temp, RAM, CPU) are always fresh.
- **Timeout** per command (8 s) so nothing hangs the daemon.
- For the popup, it evaluates **by priority and stops at the first one that triggers**.
- Checks with missing tools **don't even run**.

## Technical Notes

- `gtk4-layer-shell` must be loaded before `libwayland` when used from
  Python; `archie.py` re-executes itself with `LD_PRELOAD` to guarantee this.
- The system Python is required (`/usr/bin/python3`), not one from `mise`/`pyenv`, because
  that's where PyGObject lives. The service, shebang and wrapper already point to it.
- `python-yaml` is recommended but not mandatory: there is a fallback mini-parser
  for the subset of `archie_checks.yaml`.

### Test the logic without GUI

```bash
/usr/bin/python3 monitor.py     # evaluates all checks and prints the status
```