"""The Panasonic WIFAN integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .api import ApiClient
from .const import DOMAIN, PLATFORMS, CONF_USERNAME, CONF_PASSWORD

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Panasonic WIFAN component from configuration.yaml."""
    if DOMAIN not in config:
        return True

    # For configuration.yaml setup, we'd need credentials there too
    # This is primarily for config flow, so we'll just return True here
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Panasonic WIFAN from a config entry."""
    # Get credentials from config entry
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    # Initialize API client with credentials from config entry
    api = ApiClient(username, password)

    # Get list of fans
    fans = await api.get_devices()

    # Read state once here rather than in each platform: the light platform
    # needs it to know which devices have a light, and a second poll would
    # double the cloud traffic for nothing.
    try:
        states = await api.get_state_for_fans(fans)
    except Exception as err:  # noqa: BLE001 - a failed read must not block setup
        _LOGGER.error("Could not read initial state: %s", err)
        states = {}

    # Store API client, fans and their initial state
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "fans": fans,
        "states": states,
    }

    # Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Clean up resources and remove API client from hass.data
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        api = data["api"]
        # Close aiohttp session
        await api.session.close()

    return unload_ok
