from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType

from custom_components.unifi_protect_alarm_hub.api import (
    AlarmHubAuthError,
    AlarmHubConnectionError,
)
from custom_components.unifi_protect_alarm_hub.const import DOMAIN

USER_INPUT = {
    CONF_HOST: "192.168.0.103",
    CONF_PORT: 443,
    CONF_API_KEY: "k",
    CONF_VERIFY_SSL: False,
}


async def _run(hass, side_effect=None, return_value=None):
    with patch(
        "custom_components.unifi_protect_alarm_hub.config_flow.AlarmHubApiClient"
    ) as cls:
        cls.return_value.async_get_alarm_hubs = AsyncMock(
            side_effect=side_effect, return_value=return_value or []
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )


async def test_user_flow_success(hass):
    result = await _run(hass, return_value=[object()])
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.0.103"


async def test_user_flow_invalid_auth(hass):
    result = await _run(hass, side_effect=AlarmHubAuthError("bad"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass):
    result = await _run(hass, side_effect=AlarmHubConnectionError("down"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(hass):
    result = await _run(hass, side_effect=Exception("boom"))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
