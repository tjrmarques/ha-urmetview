"""Config and options flow for UrmetView."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ALLOW_CLOUD,
    CONF_AUTH_HASH,
    CONF_DOORBELL_TZSP,
    CONF_DOORBELL_TZSP_PORT,
    CONF_PIXEL_ASPECT,
    CONF_QUALITY,
    CONF_STREAM_AUDIO,
    CONF_RING_PREWARM,
    CONF_STREAM_IDLE_TIMEOUT,
    CONF_TALK_REPEAT,
    CONF_UID,
    DEFAULT_PIXEL_ASPECT,
    DEFAULT_STREAM_IDLE_TIMEOUT,
    PIXEL_ASPECT_OPTIONS,
    DEFAULT_TZSP_PORT,
    DOMAIN,
)
from .urmet import UrmetAuthError, UrmetError, UrmetSession
from .urmet import discovery, protocol
from .urmet.const import (
    DEFAULT_QUALITY,
    DEFAULT_TALK_REPEAT,
    DEFAULT_USERNAME,
    QUALITY_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


class DeviceNotFound(UrmetError):
    """Discovery found nothing, as opposed to finding it and failing to log in."""


class UrmetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user through setting the intercom up."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: protocol.DiscoveredDevice | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the UID and auth hash, then verify by logging in."""
        errors: dict[str, str] = {}

        if user_input is not None:
            uid = user_input[CONF_UID].strip().upper()
            auth = user_input[CONF_AUTH_HASH].strip()
            host = (user_input.get(CONF_HOST) or "").strip() or None
            port = user_input.get(CONF_PORT) or None
            allow_cloud = user_input.get(CONF_ALLOW_CLOUD, True)

            if not protocol.AUTH_HASH_RE.match(auth):
                errors[CONF_AUTH_HASH] = "invalid_auth_format"
            else:
                try:
                    protocol.split_uid(uid)
                except protocol.ProtocolError:
                    errors[CONF_UID] = "invalid_uid"

            if not errors:
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()
                try:
                    host, port = await self._async_verify(
                        uid,
                        auth,
                        user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                        host,
                        port,
                        allow_cloud,
                    )
                except UrmetAuthError:
                    errors["base"] = "invalid_auth"
                except DeviceNotFound:
                    errors["base"] = "not_found"
                except UrmetError as err:
                    _LOGGER.debug("Setup verification failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title="Urmet intercom",
                        data={
                            CONF_UID: uid,
                            CONF_AUTH_HASH: auth,
                            CONF_USERNAME: user_input.get(
                                CONF_USERNAME, DEFAULT_USERNAME
                            ),
                            CONF_HOST: host,
                            CONF_PORT: port,
                        },
                        # Carried into options so the choice made here is the
                        # one the running integration uses, and stays visible
                        # and changeable afterwards.
                        options={CONF_ALLOW_CLOUD: allow_cloud},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(user_input),
            errors=errors,
        )

    def _schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        suggested_uid = ""
        suggested_host = ""
        if self._discovered is not None:
            suggested_uid = self._discovered.uid
            suggested_host = self._discovered.host
        if user_input:
            suggested_uid = user_input.get(CONF_UID, suggested_uid)
            suggested_host = user_input.get(CONF_HOST, suggested_host)

        return vol.Schema(
            {
                vol.Required(CONF_UID, default=suggested_uid): str,
                vol.Required(CONF_AUTH_HASH, default=""): str,
                vol.Optional(CONF_HOST, default=suggested_host): str,
                vol.Optional(CONF_PORT): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Optional(CONF_ALLOW_CLOUD, default=True): bool,
            }
        )

    async def _async_verify(
        self,
        uid: str,
        auth: str,
        username: str,
        host: str | None,
        port: int | None,
        allow_cloud: bool = True,
    ) -> tuple[str, int]:
        """Prove the credentials work before creating the entry.

        Logging in for real is the only meaningful check: the hash is opaque,
        so there is nothing to validate locally beyond its shape.
        """
        # Allow the port sweep when a host is known: it is slow but fully
        # local, so "enter the IP, leave the port blank" just works even when
        # broadcast cannot cross to the device's subnet and the cloud is
        # unreachable.
        candidates = await discovery.async_find_candidates(
            uid,
            host=host,
            cached_port=int(port) if port else None,
            allow_cloud=allow_cloud,
            allow_sweep=bool(host),
        )
        if not candidates:
            raise DeviceNotFound

        last_error: UrmetError | None = None
        for candidate in candidates:
            session = UrmetSession(candidate.host, candidate.port, uid, auth, username)
            try:
                await session.async_connect()
            except UrmetAuthError:
                raise  # the hash is wrong; another address will not help
            except UrmetError as err:
                _LOGGER.debug("No session at %s: %s", candidate, err)
                last_error = err
                continue
            finally:
                # Always tear down, or the device holds the session and the
                # first real connection is refused.
                try:
                    await session.async_close()
                except UrmetError:
                    pass
            return candidate.host, candidate.port

        raise last_error or DeviceNotFound

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> UrmetOptionsFlow:
        return UrmetOptionsFlow()


class UrmetOptionsFlow(OptionsFlow):
    """Tunables that do not warrant re-authenticating."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_QUALITY,
                        default=options.get(CONF_QUALITY, DEFAULT_QUALITY),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=QUALITY_OPTIONS, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Optional(
                        CONF_STREAM_AUDIO,
                        default=options.get(CONF_STREAM_AUDIO, True),
                    ): bool,
                    vol.Optional(
                        CONF_PIXEL_ASPECT,
                        default=options.get(CONF_PIXEL_ASPECT, DEFAULT_PIXEL_ASPECT),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=PIXEL_ASPECT_OPTIONS,
                            mode=SelectSelectorMode.DROPDOWN,
                            custom_value=True,
                        )
                    ),
                    vol.Optional(
                        CONF_STREAM_IDLE_TIMEOUT,
                        default=options.get(
                            CONF_STREAM_IDLE_TIMEOUT, DEFAULT_STREAM_IDLE_TIMEOUT
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=5, max=600, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(
                        CONF_TALK_REPEAT,
                        default=options.get(CONF_TALK_REPEAT, DEFAULT_TALK_REPEAT),
                    ): NumberSelector(
                        NumberSelectorConfig(min=1, max=12, mode=NumberSelectorMode.BOX)
                    ),
                    vol.Optional(
                        CONF_DOORBELL_TZSP,
                        default=options.get(CONF_DOORBELL_TZSP, False),
                    ): bool,
                    vol.Optional(
                        CONF_ALLOW_CLOUD,
                        default=options.get(CONF_ALLOW_CLOUD, True),
                    ): bool,
                    vol.Optional(
                        CONF_RING_PREWARM,
                        default=options.get(CONF_RING_PREWARM, False),
                    ): bool,
                    vol.Optional(
                        CONF_DOORBELL_TZSP_PORT,
                        default=options.get(CONF_DOORBELL_TZSP_PORT, DEFAULT_TZSP_PORT),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=65535, mode=NumberSelectorMode.BOX
                        )
                    ),
                }
            ),
        )
