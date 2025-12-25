# -*- coding: utf-8 -*-
"""The China Southern Power Grid Statistics integration."""
from __future__ import annotations

import logging
import time

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_AUTH_TOKEN,
    CONF_DELETE_ENTITY_DATA_ON_REMOVAL,
    CONF_ELE_ACCOUNTS,
    CONF_LOGIN_TYPE,
    CONF_SETTINGS,
    CONF_UPDATED_AT,
    DOMAIN,
    SERVICE_PURGE_DEVICE_DATA,
    SERVICE_PURGE_ALL_DATA,
)
from .csg_client import (
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    InvalidCredentials,
    NotLoggedIn,
)
from .sensor import CSGCostSensor, CSGEnergySensor

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)

SERVICE_PURGE_DEVICE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up China Southern Power Grid Statistics from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # validate session, re-authenticate if needed
    client = CSGClient.load(
        {
            CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
        }
    )
    if not await hass.async_add_executor_job(client.verify_login):
        raise ConfigEntryAuthFailed("Login expired")

    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services only once (shared across all entries)
    if not hass.services.has_service(DOMAIN, SERVICE_PURGE_DEVICE_DATA):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PURGE_DEVICE_DATA,
            _handle_purge_device_data,
            schema=SERVICE_PURGE_DEVICE_DATA_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_PURGE_ALL_DATA):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PURGE_ALL_DATA,
            _handle_purge_all_data,
        )

    return True


async def _handle_purge_device_data(call: ServiceCall) -> None:
    """Handle purge device data service call."""
    hass = call.hass
    device_id = call.data["device_id"]
    await _purge_device_entity_data(hass, device_id)


async def _handle_purge_all_data(call: ServiceCall) -> None:
    """Handle purge all data service call.

    This will purge data for all entries in this integration.
    """
    hass = call.hass
    # Get all entries for this domain
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        await _purge_entry_entity_data(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug(f"Unloading entry: {entry.title}")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug(f"Unload platforms for entry: {entry.title}, success: {unload_ok}")
    hass.data[DOMAIN].pop(entry.entry_id)

    # Unregister services only if this is the last entry
    # Check after removal to avoid race conditions
    remaining_entries = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if not remaining_entries:
        if hass.services.has_service(DOMAIN, SERVICE_PURGE_DEVICE_DATA):
            hass.services.async_remove(DOMAIN, SERVICE_PURGE_DEVICE_DATA)
        if hass.services.has_service(DOMAIN, SERVICE_PURGE_ALL_DATA):
            hass.services.async_remove(DOMAIN, SERVICE_PURGE_ALL_DATA)

    return True


async def _purge_device_entity_data(hass: HomeAssistant, device_id: str) -> None:
    """Purge entity history data for a specific device."""
    import homeassistant.helpers.device_registry as dr

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(device_id)

    if not device_entry:
        _LOGGER.warning(f"Device {device_id} not found")
        return

    entity_reg = entity_registry.async_get(hass)
    entities = entity_registry.async_entries_for_device(
        entity_reg, device_id, include_disabled_entities=True
    )

    entity_ids = [ent.entity_id for ent in entities]

    if entity_ids:
        _LOGGER.info(f"Purging entity data for device {device_entry.name}: {entity_ids}")
        try:
            await hass.services.async_call(
                "recorder",
                "purge_entities",
                {"entity_id": entity_ids},
                blocking=True,
            )
            _LOGGER.info(f"Successfully purged entity data for device {device_entry.name}")
        except Exception as e:
            _LOGGER.error(f"Error purging entity data for device {device_entry.name}: {e}")


async def _purge_entry_entity_data(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Purge entity history data for all devices in this config entry."""
    entity_reg = entity_registry.async_get(hass)
    entities = entity_registry.async_entries_for_config_entry(
        entity_reg, config_entry.entry_id
    )

    entity_ids = [ent.entity_id for ent in entities]

    if entity_ids:
        _LOGGER.info(f"Purging entity data for entry {config_entry.title}: {entity_ids}")
        try:
            await hass.services.async_call(
                "recorder",
                "purge_entities",
                {"entity_id": entity_ids},
                blocking=True,
            )
            _LOGGER.info(f"Successfully purged entity data for entry {config_entry.title}")
        except Exception as e:
            _LOGGER.error(f"Error purging entity data for entry {config_entry.title}: {e}")


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove device"""
    _LOGGER.info(f"removing device {device_entry.name}")
    account_num = list(device_entry.identifiers)[0][1]

    # Get setting for deleting entity data
    delete_entity_data = config_entry.data.get(CONF_SETTINGS, {}).get(
        CONF_DELETE_ENTITY_DATA_ON_REMOVAL, False
    )

    # remove entities
    entity_reg = entity_registry.async_get(hass)
    entities = {
        ent.unique_id: ent.entity_id
        for ent in entity_registry.async_entries_for_config_entry(
            entity_reg, config_entry.entry_id
        )
        if account_num in ent.unique_id
    }

    # Purge entity history data if configured
    if delete_entity_data:
        entity_ids = list(entities.values())
        if entity_ids:
            _LOGGER.info(f"Purging entity data for removed device: {entity_ids}")
            try:
                await hass.services.async_call(
                    "recorder",
                    "purge_entities",
                    {"entity_id": entity_ids},
                    blocking=True,
                )
                _LOGGER.info(f"Successfully purged entity data for device {device_entry.name}")
            except Exception as e:
                _LOGGER.error(f"Error purging entity data: {e}")

    for entity_id in entities.values():
        entity_reg.async_remove(entity_id)

    # update config entry
    new_data = config_entry.data.copy()
    new_data[CONF_ELE_ACCOUNTS].pop(account_num)
    new_data[CONF_UPDATED_AT] = str(int(time.time() * 1000))
    hass.config_entries.async_update_entry(
        config_entry,
        data=new_data,
    )
    _LOGGER.info(
        "Removed ele account from %s: %s",
        config_entry.data[CONF_USERNAME],
        account_num,
    )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    _LOGGER.info("Removing entry: account %s", entry.data[CONF_USERNAME])

    # Get setting for deleting entity data
    delete_entity_data = entry.data.get(CONF_SETTINGS, {}).get(
        CONF_DELETE_ENTITY_DATA_ON_REMOVAL, False
    )

    # Purge all entity history data if configured
    if delete_entity_data:
        try:
            await _purge_entry_entity_data(hass, entry)
        except Exception as e:
            _LOGGER.error(f"Error purging entity data during entry removal: {e}")

    # logout
    def client_logout():
        client = CSGClient.load(
            {
                CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
            }
        )
        if client.verify_login():
            client.logout(entry.data[CONF_LOGIN_TYPE])
            _LOGGER.info("CSG account %s logged out", entry.data[CONF_USERNAME])

    await hass.async_add_executor_job(client_logout)
