"""dbus_listener.py — Archie's asynchronous D-Bus event listener.

This module listens to system and session bus signals (power, screen, session)
and relays them to the Archie brain for real-time reactivity without polling.
It aligns with the architecture of brain.py and monitor.py.
"""

import asyncio
import logging
from typing import Callable, Optional

from dbus_next.aio import MessageBus
from dbus_next import BusType

# Setup logging
logger = logging.getLogger('archie.dbus')

class DBusListener:
    """Highly optimized async listener for system and session D-Bus signals."""

    def __init__(self, brain_callback: Callable[[str, dict], None]):
        """
        Args:
            brain_callback: Function to call when a relevant event is detected.
        """
        self.callback = brain_callback
        self.system_bus: Optional[MessageBus] = None
        self.session_bus: Optional[MessageBus] = None
        self._running = False

    async def start(self):
        """Initializes buses and begins listening to signals."""
        self._running = True
        try:
            self.system_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self.session_bus = await MessageBus(bus_type=BusType.SESSION).connect()
            
            # Setup specific watchers
            await self._setup_upower_listener()
            await self._setup_login1_listener()
            
            logger.info("Archie DBus Listener started successfully.")
            
            # Keep the loop alive if this is the main entry point
            while self._running:
                await asyncio.sleep(3600)
                
        except Exception as e:
            logger.error(f"Failed to start DBus Listener: {e}")
            await self.stop()

    async def stop(self):
        """Gracefully disconnects from buses."""
        self._running = False
        if self.system_bus:
            self.system_bus.disconnect()
        if self.session_bus:
            self.session_bus.disconnect()
        logger.info("Archie DBus Listener stopped.")

    # --- UPower (Battery/Charger) ---
    async def _setup_upower_listener(self):
        """Listens for power supply status changes."""
        introspection = await self.system_bus.introspect('org.freedesktop.UPower', '/org/freedesktop/UPower')
        obj = self.system_bus.get_proxy_object('org.freedesktop.UPower', '/org/freedesktop/UPower', introspection)
        upower = obj.get_interface('org.freedesktop.UPower')
        
        # When device is added/removed or properties change
        upower.on_device_added(self._on_upower_event)
        upower.on_device_removed(self._on_upower_event)
        
        # Monitor specific battery properties
        properties = obj.get_interface('org.freedesktop.DBus.Properties')
        properties.on_properties_changed(self._on_upower_properties_changed)

    def _on_upower_event(self, device_path):
        self.callback("power_device_change", {"path": device_path})

    def _on_upower_properties_changed(self, interface, changed, invalidated):
        if interface == 'org.freedesktop.UPower':
            self.callback("power_status_change", changed)

    # --- Systemd Login1 (Session/Lock/Sleep) ---
    async def _setup_login1_listener(self):
        """Listens for session events like locking/unlocking or sleep."""
        introspection = await self.system_bus.introspect('org.freedesktop.login1', '/org/freedesktop/login1')
        obj = self.system_bus.get_proxy_object('org.freedesktop.login1', '/org/freedesktop/login1', introspection)
        manager = obj.get_interface('org.freedesktop.login1.Manager')
        
        manager.on_prepare_for_sleep(self._on_prepare_for_sleep)
        # Session signals are usually on the specific session object, but we monitor the manager for global state

    def _on_prepare_for_sleep(self, sleeping: bool):
        kind = "system_sleep" if sleeping else "system_wake"
        self.callback(kind, {"sleeping": sleeping})

# Example usage integration (Brain-like)
if __name__ == "__main__":
    def mock_brain_callback(event_type, data):
        print(f"[Archie Brain] Received Event: {event_type} | Data: {data}")

    logging.basicConfig(level=logging.INFO)
    listener = DBusListener(mock_brain_callback)
    
    try:
        asyncio.run(listener.start())
    except KeyboardInterrupt:
        asyncio.run(listener.stop())
