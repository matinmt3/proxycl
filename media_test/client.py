"""Audited Telethon 1.45.0 adapter with no history/update or CDN fallback."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from media_test.models import CdnUnsupported, FileUnavailable
from media_test.transport import connection_factory, require_telethon


@dataclass(repr=False)
class Credentials:
    api_id: int
    api_hash: str = field(repr=False)
    phone: str = field(repr=False)


@dataclass(repr=False)
class AuthSnapshot:
    """Only the auth key is reused; no senders, packet ciphers or entities cross routes."""

    dc_id: int
    server: str
    port: int
    key: bytes = field(repr=False)

    @classmethod
    def capture(cls, client):
        session = client.session
        if session is None or session.auth_key is None or not session.auth_key.key:
            return None
        return cls(session.dc_id, session.server_address, session.port, bytes(session.auth_key.key))


def client_class():
    telethon = require_telethon()
    from telethon import functions

    allowed = {
        functions.auth.SendCodeRequest,
        functions.auth.SignInRequest,
        functions.auth.CheckPasswordRequest,
        functions.auth.ExportAuthorizationRequest,
        functions.auth.ImportAuthorizationRequest,
        functions.auth.LogOutRequest,
        functions.account.GetPasswordRequest,
        functions.help.GetConfigRequest,
        functions.users.GetUsersRequest,
        functions.contacts.ResolveUsernameRequest,
        functions.channels.GetChannelsRequest,
        functions.channels.GetMessagesRequest,
        functions.messages.GetMessagesRequest,
        functions.upload.GetFileRequest,
    }

    class MediaClient(telethon.TelegramClient):
        async def _on_login(self, user):
            # Upstream normally calls updates.GetDifference here, even when
            # receive_updates=False. Do not retrieve the account's messages.
            self._mb_entity_cache.set_self_user(user.id, user.bot, user.access_hash)
            self._authorized = True
            return user

        async def _update_loop(self):
            # connect() always schedules an update task. No update polling or
            # queue consumption is necessary for a single explicit download.
            await asyncio.Event().wait()

        async def _get_cdn_client(self, cdn_redirect):
            # Upstream creates a new client with default connection arguments.
            # Reject before that construction; no direct fallback is allowed.
            raise CdnUnsupported("cdn_unsupported")

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            requests = request if isinstance(request, (list, tuple)) else [request]
            if any(type(item) not in allowed for item in requests):
                raise ValueError("RPC outside the media measurement contract")
            return await super()._call(sender, request, ordered, flood_sleep_threshold)

    return MediaClient


def make_client(proxy, budget, credentials: Credentials, snapshot: AuthSnapshot | None = None):
    from telethon.crypto import AuthKey
    from telethon.sessions import MemorySession

    session = MemorySession()
    if snapshot is not None:
        session.set_dc(snapshot.dc_id, snapshot.server, snapshot.port)
        session.auth_key = AuthKey(bytes(snapshot.key))
    # A dedicated silent logger also covers exported senders. Do not put
    # exception text, entities, phone numbers or requests into product logs.
    logger = logging.getLogger("revmamad.private_media")
    logger.setLevel(100)
    logger.propagate = False
    logger.handlers = [logging.NullHandler()]
    return client_class()(
        session,
        credentials.api_id,
        credentials.api_hash,
        connection=connection_factory(proxy, budget),
        proxy=None,
        timeout=15,
        request_retries=1,
        connection_retries=1,
        auto_reconnect=False,
        flood_sleep_threshold=0,
        receive_updates=False,
        catch_up=False,
        base_logger=logger,
        device_model="REVMAMAD optional media test",
        system_version="local ephemeral session",
        app_version="4.0 experimental",
    )


async def get_document(client, channel: str, message_id: int):
    from telethon import types

    entity = await client.get_entity(channel)
    if not isinstance(entity, types.Channel) or not entity.username:
        raise FileUnavailable("public_channel_required")
    message = await client.get_messages(entity, ids=message_id)
    document = getattr(getattr(message, "media", None), "document", None)
    if not isinstance(document, types.Document) or document.size <= 0:
        raise FileUnavailable("document_unavailable")
    return document


async def disconnect_client(client):
    if client is not None:
        try:
            await asyncio.wait_for(client.disconnect(), 3.0)
        except (Exception, asyncio.CancelledError):
            # Close known connections even when sender/task cleanup timed out.
            senders = [getattr(client, "_sender", None)]
            senders.extend(sender for _, sender in getattr(client, "_borrowed_senders", {}).values())
            for sender in senders:
                connection = getattr(sender, "_connection", None)
                if connection is not None:
                    try:
                        await connection.disconnect()
                    except Exception:
                        pass
