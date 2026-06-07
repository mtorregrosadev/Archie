"""dbus_listener.py — Archie's native GDBus event listener.

This module uses Gio.DBus to listen for system signals (power, sleep)
directly within the GLib/GTK main loop, eliminating the need for asyncio.
"""

import logging
from typing import Callable
from gi.repository import Gio, GLib

logger = logging.getLogger('archie.dbus')

class DBusListener:
    """Native GDBus listener for system and session signals."""

    def __init__(self, brain_callback: Callable[[str, dict], None]):
        self.callback = brain_callback
        self.system_bus = None
        self._subs = []

    def start(self):
        """Initializes buses and begins listening via Gio."""
        try:
            self.system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            
            # 1. UPower: Battery/Charger
            self._subs.append(self.system_bus.signal_subscribe(
                "org.freedesktop.UPower",
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                "/org/freedesktop/UPower",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_upower_changed,
                None
            ))

            # 2. Login1: System Sleep/Wake
            self._subs.append(self.system_bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
                None
            ))

            logger.info("Archie Native DBus Listener started.")
        except Exception as e:
            logger.error(f"Failed to start Native DBus Listener: {e}")

    def stop(self):
        """Disconnects all subscriptions."""
        if self.system_bus:
            for sub_id in self._subs:
                self.system_bus.signal_unsubscribe(sub_id)
        self._subs = []
        logger.info("Archie Native DBus Listener stopped.")

    def _on_upower_changed(self, conn, sender, path, iface, signal, params, user_data):
        # params is a GVariant (tuple) containing (interface_name, changed_properties, invalidated_properties)
        if len(params) >= 2:
            iface_name = params[0]
            if iface_name == "org.freedesktop.UPower":
                changed = params[1]
                # Convert GVariant dict to Python dict if possible or just notify brain to re-sample
                self.callback("power_status_change", {})

    def _on_prepare_for_sleep(self, conn, sender, path, iface, signal, params, user_data):
        if len(params) >= 1:
            sleeping = params[0]
            kind = "system_sleep" if sleeping else "system_wake"
            self.callback(kind, {"sleeping": bool(sleeping)})
