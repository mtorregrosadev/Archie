# 🐱 Archie

Una mascota flotant minúscula per **Hyprland / Wayland** que vigila el sistema
i, només quan cal, apareix a la cantonada inferior dreta amb un suggeriment
d'optimització i dos botons: **Arregla-ho** i **Ara no**.

La resta del temps és invisible. Mai molesta més d'un cop cada 15 minuts.

```
 /\_/\
( o.o )   "Swappiness massa alt per SSD (>10). El redueixo a 10?"
 > ^ <      [ Arregla-ho ]  [ Ara no ]
```

![Exemple d'Archie](assets/archie_example.png)

## Comandes

```bash
archie            # (sense args) arrenca el daemon — normalment via systemd
archie status     # taula a la consola: ✓ OK / ✗ ALERTA / · omès, per a TOTS els checks
archie brain      # transparència: què ha mostrejat i après l'Archie de tu
archie undo       # desfà totes les automatitzacions que l'Archie ha aplicat
archie reset [all]# esborra els snoozes (amb 'all', també l'aprenentatge)
archie check      # comprova ara mateix i ho mostra com a bombolla (o "tot OK")
archie demo       # bombolla d'exemple que NO desapareix sola (per veure-la bé)
archie help
```

## Com es comporta

- **Daemon**: comprova periòdicament. Mostra com a molt **1 suggeriment cada 15 min**
  (els **crítics** salten l'espera i avisen a l'instant).
- **Durada**: la bombolla es queda **20 s** (configurable amb `ARCHIE_TIMEOUT`) i
  **el temporitzador es pausa mentre hi tens el ratolí a sobre** — així no marxa
  mentre llegeixes. `Esc` o "Ara no" la tanca.
- **Silenci adaptatiu**: en fer "Ara no", el tema calla amb **backoff exponencial**
  (1 h → 2 h → 4 h… fins a 1 setmana). Com més l'ignores, menys t'empipa.
- **Estat** a `~/.local/state/archie/state.json`: últim avís, temes silenciats,
  optimitzacions fetes (`once`), **historial de mètriques**, **aprenentatge** i
  **automatitzacions actives**.

## El cervell (`brain.py`) — la part "intel·ligent"

Archie no es limita a mirar una instantània: **mostreja tendències, aprèn de tu i
actua sol** en coses segures i reversibles.

- **Mostreig de tendències**: cada cicle desa bateria, càrrega i RAM. Per decidir
  si estàs *realment* en repòs mira **diverses mostres seguides** (no l'instant, que
  enganya entre frames d'un render).
- **Aprenentatge** (`archie brain`): recorda què acceptes i què ignores. Com més
  ignores → més silenci (backoff). Com més acceptes un fix **segur i reversible**
  (marcat `auto: true`) → quan en portes **3 seguits**, l'Archie **el fa sol** i
  només t'ensenya un **«Desfés»**. Si el desfàs, deixa de fer-lo automàticament.
- **Accions fantasma** (contextuals i reversibles): quan estàs amb **bateria** i
  l'equip està en repòs, pot — només si tens les eines — posar el **perfil
  d'estalvi**, **apagar el Bluetooth** que no uses, **atenuar la pantalla** o
  **treure les animacions** d'Hyprland. **Es desfan soles quan endolles** el
  carregador. Tot queda llistat a `archie brain` i revertible amb `archie undo`.

## Els checks viuen al YAML

Tot el que Archie comprova està a **`archie_checks.yaml`** (editable):

```yaml
- id: cpu_governor_wrong
  category: cpu
  message: "CPU en mode extrem. La poso a 'balanced'?"
  detect: "...scaling_governor | grep -qv 'balanced\\|power-saver'"  # codi 0 => ALERTA
  fix:  "powerprofilesctl set balanced"     # o null si és informatiu
  undo: "powerprofilesctl set performance"  # com revertir-ho
  auto: true                                # l'Archie ho pot acabar fent sol
  # altres opcionals: label: "Mostra-ho"   once: true   critical: true
```

Si el `message` o el `fix` contenen `{}`, s'hi injecta la **sortida del `detect`**
(p. ex. el nom del procés que crema CPU) sense malmetre la plantilla.

Hi ha ~40 checks: tèrmica, CPU/energia, RAM, SSD, Wayland-vs-X11, arrencada,
bateria, seguretat, GPU, xarxa i rendiment. `archie status` te'ls ensenya tots.

**Si una eina de `detect` no està instal·lada, el check s'OMET** (no falla);
`archie status` ho marca amb `·`.

### Com s'apliquen els fixos

En fer **Arregla-ho**, el `fix` s'executa **dins un terminal que es queda obert**
amb el resultat. Així pots escriure la contrasenya (`sudo`) i veure si ha anat bé.
Els `fix: null` són només informatius (botó "Entesos").

## Instal·lació

```bash
./install.sh
```

Això: instal·la dependències amb `pacman` (`gtk4`, `gtk4-layer-shell`,
`python-gobject`, `python-yaml`), copia l'app i el YAML a `~/.local/share/archie/`,
posa la comanda `archie` a `~/.local/bin/`, i activa el servei d'usuari
`~/.config/systemd/user/archie.service`.

Les eines de detecció opcionals (lm_sensors, pacman-contrib, bind, mesa-utils…)
i `mbpfan` (AUR) les recomana però no les força. Cal un terminal (alacritty,
kitty, foot…) perquè els fixos amb `sudo` puguin demanar contrasenya.

```bash
systemctl --user status archie.service        # estat del servei
journalctl --user -u archie.service -f         # logs
systemctl --user disable --now archie.service  # desinstal·lar el servei
```

## Rendiment (com està optimitzat)

- El sweep periòdic corre en un **fil de fons** → la UI no es bloqueja mai.
- **Caching per check** segons categoria: els cars (`checkupdates`, `dig`, boot…)
  no es repeteixen cada cicle; els de temps real (temp, RAM, CPU) sempre frescos.
- **Timeout** per comanda (8 s) perquè res no pengi el daemon.
- Per al popup s'avalua **per prioritat i es para al primer que salta**.
- Els checks amb l'eina absent **ni s'executen**.

## Notes tècniques

- `gtk4-layer-shell` s'ha de carregar abans que `libwayland` quan s'usa des de
  Python; `archie.py` es reexecuta sol amb `LD_PRELOAD` per garantir-ho.
- Cal el Python del sistema (`/usr/bin/python3`), no un de `mise`/`pyenv`, perquè
  és on viu PyGObject. El servei, el shebang i el wrapper ja hi apunten.
- `python-yaml` és recomanat però no obligatori: hi ha un mini-parser de
  fallback per al subconjunt d'`archie_checks.yaml`.

### Provar la lògica sense GUI

```bash
/usr/bin/python3 monitor.py     # avalua tots els checks i imprimeix l'estat
```
