"""Tests for Mitsubishi Comfort Cloud climate entities."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, call

from freezegun.api import FrozenDateTimeFactory
from mitsubishi_comfort.exceptions import (
    AuthenticationError,
    CommandError,
    DeviceConnectionError,
)
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_SWING_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    HVACMode,
)
from homeassistant.components.mitsubishi_comfort_cloud.const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import MOCK_SERIAL

ENTITY_ID = "climate.living_room"


async def _refresh(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    interval: int | None = None,
) -> None:
    """Refresh the reported cloud state on the polling interval."""
    freezer.tick(
        DEFAULT_SCAN_INTERVAL if interval is None else timedelta(seconds=interval)
    )
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _reported_state(state: State) -> tuple[str, dict[str, Any]]:
    """Return climate values without command-status attributes."""
    attributes = dict(state.attributes)
    attributes.pop("command_status", None)
    attributes.pop("pending_commands", None)
    attributes.pop("unconfirmed_commands", None)
    return state.state, attributes


def _pending_commands_from(
    commands: list[dict[str, str | float]],
) -> dict[str, str | float]:
    """Map cloud fields to the pending command attribute names."""
    pending_keys = {
        "operationMode": "mode",
        "spCool": "cool_setpoint",
        "spHeat": "heat_setpoint",
    }
    return {
        pending_keys[field]: value
        for command in commands
        for field, value in command.items()
    }


@pytest.mark.parametrize(
    (
        "service",
        "data",
        "command",
        "expected_mode",
        "expected_attributes",
        "expected_pending_commands",
    ),
    [
        pytest.param(
            "set_hvac_mode",
            {ATTR_HVAC_MODE: HVACMode.HEAT},
            {"operationMode": "heat"},
            HVACMode.HEAT,
            {ATTR_TEMPERATURE: 21.0},
            {"mode": "heat"},
            id="mode",
        ),
        pytest.param(
            "set_temperature",
            {ATTR_TEMPERATURE: 25},
            {"spCool": 25.0},
            HVACMode.COOL,
            {ATTR_TEMPERATURE: 25.0},
            {"cool_setpoint": 25.0},
            id="temperature",
        ),
        pytest.param(
            "set_fan_mode",
            {ATTR_FAN_MODE: "low"},
            {"fanSpeed": "low"},
            HVACMode.COOL,
            {ATTR_FAN_MODE: "low"},
            {"fan_speed": "low"},
            id="fan",
        ),
        pytest.param(
            "set_swing_mode",
            {ATTR_SWING_MODE: "swing"},
            {"airDirection": "swing"},
            HVACMode.COOL,
            {ATTR_SWING_MODE: "swing"},
            {"vane_direction": "swing"},
            id="vane",
        ),
        pytest.param(
            "turn_off",
            {},
            {"operationMode": "off"},
            HVACMode.OFF,
            {ATTR_TEMPERATURE: None, "hvac_action": "off"},
            {"mode": "off"},
            id="off",
        ),
    ],
)
async def test_cloud_commands(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
    service: str,
    data: dict[str, str | float],
    command: dict[str, str | float],
    expected_mode: HVACMode,
    expected_attributes: dict[str, str | float | None],
    expected_pending_commands: dict[str, str | float],
) -> None:
    """Accepted cloud commands only change displayed state after confirmation."""
    mock_cloud_account.get_device_profile.return_value["hasVaneSwing"] = True
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        service,
        {ATTR_ENTITY_ID: ENTITY_ID, **data},
        blocking=True,
    )
    mock_cloud_account.send_command.assert_awaited_once_with(MOCK_SERIAL, command)
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    assert state.attributes["pending_commands"] == expected_pending_commands
    assert state.attributes["command_status"] == "pending"

    await _refresh(hass, freezer)
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    assert state.attributes["pending_commands"] == expected_pending_commands
    assert state.attributes["command_status"] == "pending"

    mock_cloud_account.get_device_details.return_value.update(command)
    await _refresh(hass, freezer)
    state = hass.states.get(ENTITY_ID)
    assert state.state == expected_mode
    for attribute, value in expected_attributes.items():
        assert state.attributes[attribute] == value
    assert state.attributes["pending_commands"] == {}
    assert state.attributes["command_status"] == "reported"


async def test_cloud_command_polling_uses_backoff(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Accepted commands poll quickly, then return to normal cadence."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = next(iter(mock_config_entry.runtime_data.values()))

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_fan_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_FAN_MODE: "low"},
        blocking=True,
    )
    assert coordinator.update_interval == timedelta(seconds=1)

    for interval, next_interval in zip(
        (1, 2, 4, 8, 16),
        (2, 4, 8, 16, int(DEFAULT_SCAN_INTERVAL.total_seconds())),
        strict=True,
    ):
        await _refresh(hass, freezer, interval)
        assert coordinator.update_interval == timedelta(seconds=next_interval)

    state = hass.states.get(ENTITY_ID)
    assert state.attributes["command_status"] == "unconfirmed"
    assert state.attributes["pending_commands"] == {}
    assert state.attributes["unconfirmed_commands"] == {"fan_speed": "low"}

    mock_cloud_account.get_device_details.return_value["fanSpeed"] = "low"
    await _refresh(hass, freezer)
    state = hass.states.get(ENTITY_ID)
    assert state.attributes["command_status"] == "reported"
    assert state.attributes["pending_commands"] == {}
    assert state.attributes["unconfirmed_commands"] == {}


async def test_cloud_command_polling_stops_when_reported(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Stop accelerated polling as soon as the command is reported."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = next(iter(mock_config_entry.runtime_data.values()))

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_fan_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_FAN_MODE: "low"},
        blocking=True,
    )
    mock_cloud_account.get_device_details.return_value["fanSpeed"] = "low"

    await _refresh(hass, freezer, 1)

    state = hass.states.get(ENTITY_ID)
    assert state.attributes["command_status"] == "reported"
    assert state.attributes["pending_commands"] == {}
    assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL


async def test_cloud_command_polling_backs_off_after_failed_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Continue the bounded backoff after a transient polling failure."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = next(iter(mock_config_entry.runtime_data.values()))

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_fan_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_FAN_MODE: "low"},
        blocking=True,
    )
    mock_cloud_account.get_device_details.side_effect = DeviceConnectionError(
        "temporary network failure"
    )

    await _refresh(hass, freezer, 1)

    assert coordinator.update_interval == timedelta(seconds=2)

    mock_cloud_account.get_device_details.side_effect = None
    await _refresh(hass, freezer, 2)

    assert coordinator.update_interval == timedelta(seconds=4)


async def test_cloud_command_stays_pending_after_failed_poll(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed poll cannot confirm a command from cached device state."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_fan_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_FAN_MODE: "auto"},
        blocking=True,
    )
    mock_cloud_account.get_device_details.side_effect = DeviceConnectionError(
        "temporary network failure"
    )

    await _refresh(hass, freezer, 1)

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_UNAVAILABLE

    mock_cloud_account.get_device_details.side_effect = None
    mock_cloud_account.get_device_details.return_value["fanSpeed"] = "low"
    await _refresh(hass, freezer)

    state = hass.states.get(ENTITY_ID)
    assert state.attributes["command_status"] == "pending"
    assert state.attributes["pending_commands"] == {"fan_speed": "auto"}


@pytest.mark.parametrize(
    ("initial_mode", "commands"),
    [
        pytest.param(
            HVACMode.OFF,
            [{"operationMode": "auto"}, {"operationMode": "off"}],
            id="off-on-off",
        ),
        pytest.param(
            HVACMode.COOL,
            [{"operationMode": "off"}, {"operationMode": "auto"}],
            id="active-off-on",
        ),
    ],
)
async def test_toggles_follow_accepted_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    initial_mode: HVACMode,
    commands: list[dict[str, str]],
) -> None:
    """Repeated toggles alternate command intent before reported state changes."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = initial_mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)

    for command in commands:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "toggle",
            {ATTR_ENTITY_ID: ENTITY_ID},
            blocking=True,
        )
        state = hass.states.get(ENTITY_ID)
        assert _reported_state(state) == _reported_state(before)
        assert state.attributes["command_status"] == "pending"
        assert state.attributes["pending_commands"] == {
            "mode": command["operationMode"]
        }

    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, command) for command in commands
    ]


@pytest.mark.parametrize(
    ("initial_mode", "commands"),
    [
        pytest.param(
            HVACMode.OFF,
            [
                {"operationMode": "auto"},
                {"operationMode": "off"},
                {"operationMode": "off"},
            ],
            id="rejected-off",
        ),
        pytest.param(
            HVACMode.COOL,
            [
                {"operationMode": "off"},
                {"operationMode": "auto"},
                {"operationMode": "auto"},
            ],
            id="rejected-on",
        ),
    ],
)
async def test_rejected_toggle_preserves_accepted_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    initial_mode: HVACMode,
    commands: list[dict[str, str]],
) -> None:
    """A rejected toggle leaves the next toggle based on the last accepted mode."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = initial_mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)
    mock_cloud_account.send_command.side_effect = [None, CommandError("rejected"), None]

    await hass.services.async_call(
        CLIMATE_DOMAIN, "toggle", {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await hass.services.async_call(
            CLIMATE_DOMAIN, "toggle", {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )
    await hass.services.async_call(
        CLIMATE_DOMAIN, "toggle", {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )

    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, command) for command in commands
    ]
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    assert state.attributes["command_status"] == "pending"
    assert state.attributes["pending_commands"] == {
        "mode": commands[-1]["operationMode"]
    }


@pytest.mark.parametrize(
    ("error", "reauth_steps"),
    [
        pytest.param(CommandError("rejected"), [], id="rejected"),
        pytest.param(DeviceConnectionError("timeout"), [], id="timeout"),
        pytest.param(
            AuthenticationError("expired"), ["reauth_confirm"], id="authentication"
        ),
    ],
)
async def test_cloud_command_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    error: Exception,
    reauth_steps: list[str],
) -> None:
    """Failed commands preserve state and only auth failures request reauth."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_cloud_account.send_command.side_effect = error
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_hvac_mode",
            {ATTR_ENTITY_ID: ENTITY_ID, "hvac_mode": "heat"},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "cool"
    assert [
        (flow["step_id"], flow["context"]["entry_id"])
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    ] == [(step, mock_config_entry.entry_id) for step in reauth_steps]


async def test_cloud_poll_authentication_failure(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
) -> None:
    """Authentication failure during polling starts reauth without unloading."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    mock_cloud_account.get_device_details.side_effect = AuthenticationError("expired")
    freezer.tick(DEFAULT_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert mock_config_entry.state is ConfigEntryState.LOADED
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["step_id"] == "reauth_confirm"
    assert flows[0]["context"]["entry_id"] == mock_config_entry.entry_id


@pytest.mark.parametrize(
    ("mode", "data", "commands"),
    [
        pytest.param("heat", {"temperature": 22}, [{"spHeat": 22.0}], id="heat"),
        pytest.param(
            "auto",
            {"target_temp_low": 20, "target_temp_high": 26},
            [{"spCool": 26.0}, {"spHeat": 20.0}],
            id="range",
        ),
    ],
)
async def test_temperature_modes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    mode: str,
    data: dict[str, float],
    commands: list[dict[str, float]],
) -> None:
    """Temperature actions use the cloud setpoints for the active mode."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, **data},
        blocking=True,
    )
    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, command) for command in commands
    ]


@pytest.mark.parametrize(
    ("service", "data", "mode"),
    [
        pytest.param("set_temperature", {"temperature": 25}, "cool", id="cool"),
        pytest.param("set_temperature", {"temperature": 20}, "heat", id="heat"),
        pytest.param("set_fan_mode", {"fan_mode": "auto"}, "cool", id="fan"),
        pytest.param("set_swing_mode", {"swing_mode": "auto"}, "cool", id="vane"),
    ],
)
async def test_action_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    service: str,
    data: dict[str, str | float],
    mode: str,
) -> None:
    """Cloud failures surface as action errors without optimistic state changes."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)
    mock_cloud_account.send_command.side_effect = DeviceConnectionError("offline")
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            service,
            {ATTR_ENTITY_ID: ENTITY_ID, **data},
            blocking=True,
        )
    assert hass.states.get(ENTITY_ID) == before


@pytest.mark.parametrize(
    ("results", "expected_low", "expected_high"),
    [
        pytest.param([CommandError("rejected"), None], 20.0, 24.0, id="cool-fails"),
        pytest.param([None, CommandError("rejected")], 21.0, 26.0, id="heat-fails"),
    ],
)
async def test_partial_temperature_failure(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
    results: list[CommandError | None],
    expected_low: float,
    expected_high: float,
) -> None:
    """Both setpoints remain confirmed when only one command is accepted."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = "auto"
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)
    mock_cloud_account.send_command.side_effect = results
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {
                ATTR_ENTITY_ID: ENTITY_ID,
                "target_temp_low": 20,
                "target_temp_high": 26,
            },
            blocking=True,
        )
    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, {"spCool": 26.0}),
        call(MOCK_SERIAL, {"spHeat": 20.0}),
    ]
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    expected_pending_commands = (
        {"heat_setpoint": 20.0} if expected_low == 20.0 else {"cool_setpoint": 26.0}
    )
    assert state.attributes["pending_commands"] == expected_pending_commands
    assert state.attributes["command_status"] == "pending"

    await _refresh(hass, freezer)
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    assert state.attributes["pending_commands"] == expected_pending_commands

    mock_cloud_account.get_device_details.return_value.update(
        spHeat=expected_low, spCool=expected_high
    )
    await _refresh(hass, freezer)
    state = hass.states.get(ENTITY_ID)
    assert state.attributes["target_temp_low"] == expected_low
    assert state.attributes["target_temp_high"] == expected_high
    assert state.attributes["command_status"] == "reported"
    assert state.attributes["pending_commands"] == {}


async def test_poll_replaces_pending_command_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stale poll restores temperature routing to the reported mode."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_hvac_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, "hvac_mode": "heat"},
        blocking=True,
    )
    assert hass.states.get(ENTITY_ID).state == "cool"
    assert hass.states.get(ENTITY_ID).attributes["pending_commands"] == {"mode": "heat"}
    await _refresh(hass, freezer)
    assert hass.states.get(ENTITY_ID).state == "cool"
    assert mock_cloud_account.get_device_details.await_count == 2
    assert hass.states.get(ENTITY_ID).attributes["pending_commands"] == {"mode": "heat"}
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 25},
        blocking=True,
    )
    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, {"operationMode": "heat"}),
        call(MOCK_SERIAL, {"spCool": 25.0}),
    ]
    assert hass.states.get(ENTITY_ID).attributes["pending_commands"] == {
        "mode": "heat",
        "cool_setpoint": 25.0,
    }


@pytest.mark.parametrize("mode", ["off", "heat", "auto", "dry"])
async def test_climate_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    snapshot: SnapshotAssertion,
    mode: str,
) -> None:
    """Report cloud capabilities, ranges, and action without inferring activity."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID) == snapshot


@pytest.mark.parametrize("mode", ["auto", "autoCool", "autoHeat"])
@pytest.mark.parametrize(
    ("auto_min", "auto_max", "low", "high"),
    [
        pytest.param(None, None, 16, 30, id="heat-cool-bounds"),
        pytest.param(10, 35, 10, 35, id="wider-auto-bounds"),
    ],
)
async def test_automatic_temperature_bounds(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
    mode: str,
    auto_min: float | None,
    auto_max: float | None,
    low: float,
    high: float,
) -> None:
    """All automatic modes accept both setpoints at the supported boundaries."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = mode
    profile = mock_cloud_account.get_device_profile.return_value
    profile["minimumSetPoints"]["auto"] = auto_min
    profile["maximumSetPoints"]["auto"] = auto_max
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {
            ATTR_ENTITY_ID: ENTITY_ID,
            "target_temp_low": low,
            "target_temp_high": high,
        },
        blocking=True,
    )
    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, {"spCool": high}),
        call(MOCK_SERIAL, {"spHeat": low}),
    ]
    state = hass.states.get(ENTITY_ID)
    assert state.attributes["target_temp_low"] == 21.0
    assert state.attributes["target_temp_high"] == 24.0

    mock_cloud_account.get_device_details.return_value.update(spHeat=low, spCool=high)
    await _refresh(hass, freezer)
    state = hass.states.get(ENTITY_ID)
    assert state.attributes["target_temp_low"] == low
    assert state.attributes["target_temp_high"] == high


@pytest.mark.parametrize(
    ("mode", "data", "commands", "expected_mode"),
    [
        pytest.param(
            "cool",
            {"hvac_mode": "heat", "temperature": 22},
            [{"operationMode": "heat"}, {"spHeat": 22}],
            "heat",
            id="cool-to-heat",
        ),
        pytest.param(
            "off",
            {"hvac_mode": "heat", "temperature": 22},
            [{"operationMode": "heat"}, {"spHeat": 22}],
            "heat",
            id="off-to-heat",
        ),
        pytest.param(
            "cool",
            {"hvac_mode": "heat", "temperature": 16},
            [{"operationMode": "heat"}, {"spHeat": 16}],
            "heat",
            id="cool-to-heat-minimum",
        ),
        pytest.param(
            "heat",
            {"hvac_mode": "cool", "temperature": 30},
            [{"operationMode": "cool"}, {"spCool": 30}],
            "cool",
            id="heat-to-cool-maximum",
        ),
        pytest.param(
            "off",
            {"hvac_mode": "heat_cool", "target_temp_low": 16, "target_temp_high": 30},
            [{"operationMode": "auto"}, {"spCool": 30}, {"spHeat": 16}],
            "heat_cool",
            id="off-to-auto-range",
        ),
    ],
)
async def test_combined_mode_temperature(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    freezer: FrozenDateTimeFactory,
    mode: str,
    data: dict[str, str | float],
    commands: list[dict[str, str | float]],
    expected_mode: str,
) -> None:
    """Apply the requested mode before choosing and sending its setpoints."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, **data},
        blocking=True,
    )
    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, command) for command in commands
    ]
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    assert state.attributes["pending_commands"] == _pending_commands_from(commands)
    assert state.attributes["command_status"] == "pending"

    for command in commands:
        mock_cloud_account.get_device_details.return_value.update(command)
    await _refresh(hass, freezer)
    state = hass.states.get(ENTITY_ID)
    assert state.state == expected_mode
    assert state.attributes["command_status"] == "reported"
    assert state.attributes["pending_commands"] == {}


@pytest.mark.parametrize(
    ("initial_mode", "requested_mode", "temperature", "setpoint_field"),
    [
        pytest.param(HVACMode.COOL, HVACMode.HEAT, 16.0, "spHeat", id="cool-to-heat"),
        pytest.param(HVACMode.OFF, HVACMode.HEAT, 16.0, "spHeat", id="off-to-heat"),
        pytest.param(HVACMode.HEAT, HVACMode.COOL, 30.0, "spCool", id="heat-to-cool"),
    ],
)
async def test_temperature_follows_accepted_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    initial_mode: HVACMode,
    requested_mode: HVACMode,
    temperature: float,
    setpoint_field: str,
) -> None:
    """Separate actions route boundary setpoints using the accepted mode."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = initial_mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_hvac_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: requested_mode},
        blocking=True,
    )
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: temperature},
        blocking=True,
    )
    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, {"operationMode": requested_mode}),
        call(MOCK_SERIAL, {setpoint_field: temperature}),
    ]
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    assert state.attributes["pending_commands"] == {
        "mode": requested_mode.value,
        "heat_setpoint" if setpoint_field == "spHeat" else "cool_setpoint": temperature,
    }
    assert state.attributes["command_status"] == "pending"


async def test_rejected_mode_preserves_pending_temperature_routing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
) -> None:
    """A failed mode change cannot replace the last accepted command mode."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)
    mock_cloud_account.send_command.side_effect = [None, CommandError("rejected"), None]

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_hvac_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: HVACMode.HEAT},
        blocking=True,
    )
    with pytest.raises(HomeAssistantError, match="did not accept"):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_hvac_mode",
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: HVACMode.COOL},
            blocking=True,
        )
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 16},
        blocking=True,
    )
    assert mock_cloud_account.send_command.await_args_list == [
        call(MOCK_SERIAL, {"operationMode": "heat"}),
        call(MOCK_SERIAL, {"operationMode": "cool"}),
        call(MOCK_SERIAL, {"spHeat": 16.0}),
    ]
    state = hass.states.get(ENTITY_ID)
    assert _reported_state(state) == _reported_state(before)
    assert state.attributes["pending_commands"] == {
        "mode": "heat",
        "heat_setpoint": 16.0,
    }
    assert state.attributes["command_status"] == "pending"


@pytest.mark.parametrize(
    "hvac_mode",
    [
        pytest.param("auto", id="unmapped-mode"),
        pytest.param("heat", id="unsupported-device-mode"),
    ],
)
@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"temperature": 25}, id="single"),
        pytest.param({"target_temp_low": 20, "target_temp_high": 26}, id="range"),
    ],
)
async def test_unsupported_combined_mode_temperature(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    hvac_mode: str,
    data: dict[str, float],
) -> None:
    """Unsupported modes must fail before changing mode or setpoints."""
    mock_cloud_account.get_device_profile.return_value["hasModeHeat"] = False
    mock_cloud_account.get_device_profile.return_value["maximumSetPoints"].pop("heat")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)
    assert hvac_mode not in before.attributes["hvac_modes"]

    with pytest.raises(ServiceValidationError, match="is not valid"):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {ATTR_ENTITY_ID: ENTITY_ID, "hvac_mode": hvac_mode, **data},
            blocking=True,
        )
    mock_cloud_account.send_command.assert_not_awaited()
    assert hass.states.get(ENTITY_ID) == before


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(CommandError("rejected"), id="rejected"),
        pytest.param(DeviceConnectionError("offline"), id="connection"),
        pytest.param(AuthenticationError("expired"), id="authentication"),
    ],
)
@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"hvac_mode": "heat", "temperature": 22}, id="single"),
        pytest.param(
            {"hvac_mode": "heat_cool", "target_temp_low": 20, "target_temp_high": 26},
            id="range",
        ),
    ],
)
async def test_failed_mode_change_stops_temperature_commands(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    error: Exception,
    data: dict[str, str | float],
) -> None:
    """A failed mode command must not change any setpoints or optimistic state."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)
    mock_cloud_account.send_command.side_effect = error

    with pytest.raises(
        HomeAssistantError, match="did not accept the requested HVAC mode"
    ):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {ATTR_ENTITY_ID: ENTITY_ID, **data},
            blocking=True,
        )
    assert mock_cloud_account.send_command.await_count == 1
    assert "operationMode" in mock_cloud_account.send_command.await_args.args[1]
    assert hass.states.get(ENTITY_ID) == before


@pytest.mark.parametrize(
    ("minimums", "maximums", "expected_min", "expected_max"),
    [
        pytest.param({}, {}, 7, 35, id="unknown"),
        pytest.param({"heat": 16}, {"heat": 28}, 16, 28, id="heat-only"),
        pytest.param({"cool": 18}, {"cool": 30}, 18, 30, id="cool-only"),
        pytest.param({"auto": 10}, {"auto": 35}, 10, 35, id="auto-only"),
        pytest.param({"heat": 16}, {}, 16, 35, id="missing-maximum"),
        pytest.param({}, {"cool": 30}, 7, 30, id="missing-minimum"),
    ],
)
async def test_partial_temperature_bounds(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    minimums: dict[str, float],
    maximums: dict[str, float],
    expected_min: float,
    expected_max: float,
) -> None:
    """Use available profile bounds, defaulting only when none are reported."""
    profile = mock_cloud_account.get_device_profile.return_value
    profile["minimumSetPoints"] = minimums
    profile["maximumSetPoints"] = maximums
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.attributes["min_temp"] == expected_min
    assert state.attributes["max_temp"] == expected_max


@pytest.mark.parametrize(
    ("mode", "temperature"),
    [
        pytest.param("heat", 30, id="above-heat-maximum"),
        pytest.param("cool", 16, id="below-cool-minimum"),
    ],
)
async def test_reject_invalid_mode_setpoint(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_cloud_account: AsyncMock,
    mode: str,
    temperature: float,
) -> None:
    """The library still rejects invalid setpoints within the overall range."""
    mock_cloud_account.get_device_details.return_value["operationMode"] = mode
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    before = hass.states.get(ENTITY_ID)

    with pytest.raises(HomeAssistantError, match="did not accept"):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {ATTR_ENTITY_ID: ENTITY_ID, "temperature": temperature},
            blocking=True,
        )
    mock_cloud_account.send_command.assert_not_awaited()
    assert hass.states.get(ENTITY_ID) == before
