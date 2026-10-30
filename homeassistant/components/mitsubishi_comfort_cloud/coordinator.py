"""DataUpdateCoordinator for Mitsubishi Comfort Cloud devices."""

from collections.abc import Awaitable
from datetime import timedelta
import logging
from typing import override

from mitsubishi_comfort import CloudIndoorUnit, CommandResult
from mitsubishi_comfort.exceptions import AuthenticationError, MitsubishiComfortError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

_COMMAND_SCAN_INTERVALS = (1, 2, 4, 8, 16)

type MitsubishiComfortCloudConfigEntry = ConfigEntry[
    dict[str, MitsubishiComfortCloudCoordinator]
]


class MitsubishiComfortCloudCoordinator(DataUpdateCoordinator[CloudIndoorUnit]):
    """Coordinator to poll a single Mitsubishi device."""

    config_entry: MitsubishiComfortCloudConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MitsubishiComfortCloudConfigEntry,
        device: CloudIndoorUnit,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{device.serial}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.device = device
        self.data = device
        self._command_polls_remaining = 0
        self._command_generation = 0

    @callback
    def async_command_accepted(self) -> None:
        """Poll quickly after a command, then back off if it stays unconfirmed."""
        self._command_generation += 1
        self._command_polls_remaining = len(_COMMAND_SCAN_INTERVALS)
        self.update_interval = timedelta(seconds=_COMMAND_SCAN_INTERVALS[0])
        self._schedule_refresh()

    @property
    def command_confirmation_active(self) -> bool:
        """Return whether accelerated command confirmation polls remain."""
        return self._command_polls_remaining > 0

    @callback
    def async_stop_command_polling(self) -> None:
        """Stop accelerated polling when no commands remain pending."""
        self._command_polls_remaining = 0
        self.update_interval = DEFAULT_SCAN_INTERVAL
        self._schedule_refresh()

    @override
    async def _async_update_data(self) -> CloudIndoorUnit:
        """Poll the device and return it."""
        generation = self._command_generation
        try:
            if not await self.device.update_status():
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="update_failed",
                    translation_placeholders={"device_name": self.device.name},
                )
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="authentication_failed"
            ) from err
        except MitsubishiComfortError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"device_name": self.device.name},
            ) from err
        finally:
            if generation == self._command_generation and self._command_polls_remaining:
                self._command_polls_remaining -= 1
                if self._command_polls_remaining:
                    index = len(_COMMAND_SCAN_INTERVALS) - self._command_polls_remaining
                    self.update_interval = timedelta(
                        seconds=_COMMAND_SCAN_INTERVALS[index]
                    )
                else:
                    self.update_interval = DEFAULT_SCAN_INTERVAL
        return self.device

    async def async_command(self, command: Awaitable[CommandResult]) -> CommandResult:
        """Translate cloud command failures and request reauthentication."""
        try:
            return await command
        except AuthenticationError:
            self.config_entry.async_start_reauth(self.hass)
        except MitsubishiComfortError as err:
            _LOGGER.debug("Command failed for %s: %s", self.device.name, err)
        return CommandResult(success=False)
