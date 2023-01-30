# SPDX-FileCopyrightText: 2023-present MTRNord <support@nordgedanken.dev>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import getpass
import json
import os
import sys
from nio.events import to_device
from nio import (
    AsyncClient,
    ToDeviceCallAnswerEvent,
    CallInviteEvent,
    CallCandidatesEvent,
    CallHangupEvent,
    ToDeviceCallInviteEvent,
    ToDeviceCallCandidatesEvent,
    ToDeviceCallHangupEvent,
    MSC3401CallEvent,
    CallMemberEvent,
    RoomMessageText,
    MatrixRoom,
    InviteEvent,
    ProfileGetDisplayNameResponse,
    LoginResponse,
    AsyncClientConfig,
)

from logbook import Logger, StreamHandler
import logbook

from matrix_call_multitrack_recorder.recorder import Recorder

StreamHandler(sys.stdout).push_application()
logger = Logger(__name__)
logger.level = logbook.INFO
# crypto.logger.level = logbook.DEBUG
to_device.logger.level = logbook.DEBUG

# file to store credentials in case you want to run program multiple times
CONFIG_FILE = "credentials.json"  # login credentials JSON file
# directory to store persistent data for end-to-end encryption
STORE_PATH = "./store/"  # local directory


class RecordingBot:
    client: AsyncClient
    loop: asyncio.AbstractEventLoop
    recorder: Recorder

    def __init__(self, client) -> None:
        self.client = client
        self.loop = asyncio.get_event_loop()
        self.recorder = Recorder(self.client)

    async def start(self) -> None:
        logger.info("Starting client")
        await self.recorder.start()

        if self.client.should_upload_keys:
            await self.client.keys_upload()
            logger.debug("Uploaded keys")

        self.client.add_event_callback(self.message_callback, RoomMessageText)  # type: ignore
        self.client.add_event_callback(self.call_candidates, CallCandidatesEvent)  # type: ignore
        self.client.add_event_callback(self.call_invite, CallInviteEvent)  # type: ignore
        self.client.add_event_callback(self.call_hangup, CallHangupEvent)  # type: ignore
        self.client.add_event_callback(self.msc3401_call, MSC3401CallEvent)  # type: ignore
        self.client.add_event_callback(self.msc3401_call_member, CallMemberEvent)  # type: ignore

        self.client.add_to_device_callback(
            self.to_device_call_candidates, (ToDeviceCallCandidatesEvent,)  # type: ignore
        )
        self.client.add_to_device_callback(
            self.to_device_call_invite, (ToDeviceCallInviteEvent,)  # type: ignore
        )
        self.client.add_to_device_callback(
            self.to_device_call_hangup, (ToDeviceCallHangupEvent,)  # type: ignore
        )
        self.client.add_to_device_callback(
            self.call_answer, (ToDeviceCallAnswerEvent,)  # type: ignore
        )

        self.client.add_event_callback(self.cb_autojoin_room, InviteEvent)  # type: ignore
        logger.info("Listening for calls")

        await self.client.sync_forever(
            timeout=30000, full_state=True, set_presence="online"
        )

    async def stop(self) -> None:
        logger.info("Stopping client")
        await self.recorder.stop()
        await self.client.close()

    async def message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Handles incoming messages."""

        async def command_handling() -> None:
            if event.body.startswith("!help"):
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.notice",
                        "body": "Please note that commands only work with the new voip (Element-Call)\n\nCommands:\n!help - Shows this help\n!stop - Stops the recording\n!start - Starts the recording",
                    },
                    ignore_unverified_devices=True,
                )
                await self.client.update_receipt_marker(room.room_id, event.event_id)
            elif event.body.startswith("!stop"):
                await self.recorder.leave_call(room)
                await self.client.update_receipt_marker(room.room_id, event.event_id)

            elif event.body.startswith("!start"):
                await self.recorder.join_call(room)
                await self.client.update_receipt_marker(room.room_id, event.event_id)

        asyncio.create_task(command_handling())

    # TODO: Negotiate new streams
    # TODO: Handle m.call.negotiate to-device events
    # TODO: Send m.call.negotiate events with type offer
    async def msc3401_call_member(
        self, room: MatrixRoom, event: CallMemberEvent
    ) -> None:
        """Handles incoming MSC3401 call members."""
        if event.sender == self.client.user_id:
            return

        # logger.info(f"MSC3401 call member event: {event}")
        if not event.calls:
            await self.recorder.remove_connection(room)
            self.recorder.remove_other(event.sender)

        for call in event.calls:
            for device in call["m.devices"]:
                if device["device_id"] != self.client.device_id:
                    self.recorder.add_call(call["m.call_id"], room)
                    self.recorder.track_others(
                        call["m.call_id"],
                        device["device_id"],
                        event.sender,
                        device["session_id"],
                    )
            # TODO: Can I reuse the same connection? Do I have the info needed? Is it a new connection? How do I see if it changed?
            # asyncio.create_task(self.handle_call_invite(event, room))

    async def msc3401_call(self, room: MatrixRoom, event: MSC3401CallEvent) -> None:
        """Handles incoming MSC3401 calls."""
        self.recorder.add_call(event.state_key, room)

    async def call_invite(self, room: MatrixRoom, event: CallInviteEvent) -> None:
        """Handles incoming call invites."""

        asyncio.create_task(self.recorder.handle_call_invite(event, room))

    async def call_answer(self, event: ToDeviceCallAnswerEvent) -> None:
        """Handles incoming call answers."""

        asyncio.create_task(self.recorder.handle_call_answer(event))

    async def to_device_call_invite(self, event: CallInviteEvent) -> None:
        """Handles incoming call invites."""
        logger.info("Received to-device call invite")

        asyncio.create_task(self.recorder.handle_call_invite(event, None))

    async def call_candidates(
        self, room: MatrixRoom, event: CallCandidatesEvent
    ) -> None:
        """Handles incoming call candidates."""

        asyncio.create_task(self.recorder.handle_call_candidates(room, event))

    async def to_device_call_candidates(self, event: CallCandidatesEvent) -> None:
        """Handles incoming call candidates."""
        logger.info("Received to-device call candidates")

        asyncio.create_task(self.recorder.handle_call_candidates(None, event))

    async def call_hangup(self, room: MatrixRoom, event: CallHangupEvent) -> None:
        """Handles call hangups."""
        asyncio.create_task(self.recorder.handle_call_hangup(room, event))

    async def to_device_call_hangup(self, event: CallHangupEvent) -> None:
        """Handles call hangups."""
        asyncio.create_task(self.recorder.handle_call_hangup(None, event))

    async def cb_autojoin_room(self, room: MatrixRoom, event: InviteEvent):
        """Callback to automatically joins a Matrix room on invite.

        Arguments:
            room {MatrixRoom} -- Provided by nio
            event {InviteEvent} -- Provided by nio
        """

        async def join_task():
            await self.client.join(room.room_id)
            sender_display_name = await self.client.get_displayname(event.sender)
            if isinstance(sender_display_name, ProfileGetDisplayNameResponse):
                response = f"Hello, I am a bot that records calls. Use !help to see available commands. I was invited by {sender_display_name.displayname}"
            else:
                response = f"Hello, I am a bot that records calls. Use !help to see available commands. I was invited by {event.sender}"
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.text",
                    "body": response,
                },
                ignore_unverified_devices=True,
            )

        asyncio.create_task(join_task())

        logger.info(f"Joining room {room.room_id} on invite from {event.sender}")


def write_details_to_disk(resp: LoginResponse, homeserver) -> None:
    """Writes the required login details to disk so we can log in later without
    using a password.

    Arguments:
        resp {LoginResponse} -- the successful client login response.
        homeserver -- URL of homeserver, e.g. "https://matrix.example.org"
    """
    # open the config file in write-mode
    with open(CONFIG_FILE, "w", encoding="utf8") as f:
        # write the login details to disk
        json.dump(
            {
                "homeserver": homeserver,  # e.g. "https://matrix.example.org"
                "user_id": resp.user_id,  # e.g. "@user:example.org"
                "device_id": resp.device_id,  # device ID, 10 uppercase letters
                "access_token": resp.access_token,  # cryptogr. access token
            },
            f,
        )


async def login() -> AsyncClient:
    client_config = AsyncClientConfig(
        store_sync_tokens=True,
        encryption_enabled=True,
    )
    # If there are no previously-saved credentials, we'll use the password
    if not os.path.exists(CONFIG_FILE):
        logger.info(
            "First time use. Did not find credential file. Asking for "
            "homeserver, user, and password to create credential file."
        )
        homeserver = "https://matrix.midnightthoughts.space"
        homeserver = input(f"Enter your homeserver URL: [{homeserver}] ")

        if not (homeserver.startswith("https://") or homeserver.startswith("http://")):
            homeserver = "https://" + homeserver

        user_id = "@user:midnightthoughts.space"
        user_id = input(f"Enter your full user ID: [{user_id}] ")

        device_name = "matrix-call-multitrack-recorder"
        # device_name = input(f"Choose a name for this device: [{device_name}] ")

        if not os.path.exists(STORE_PATH):
            os.makedirs(STORE_PATH)

        client = AsyncClient(
            homeserver,
            user_id,
            store_path=STORE_PATH,
            config=client_config,
        )
        pw = getpass.getpass()

        resp = await client.login(pw, device_name=device_name)

        # check that we logged in succesfully
        if isinstance(resp, LoginResponse):
            write_details_to_disk(resp, homeserver)
            logger.info(
                "Logged in using a password. Credentials were stored. "
                "On next execution the stored login credentials will be used."
            )
        else:
            logger.info(f'homeserver = "{homeserver}"; user = "{user_id}"')
            logger.error(f"Failed to log in: {resp}")
            sys.exit(1)

    # Otherwise the config file exists, so we'll use the stored credentials
    else:
        # open the file in read-only mode
        with open(CONFIG_FILE, "r", encoding="utf8") as f:
            config = json.load(f)
            client = AsyncClient(
                config["homeserver"],
                config["user_id"],
                device_id=config["device_id"],
                store_path=STORE_PATH,
                config=client_config,
            )

            client.restore_login(
                user_id=config["user_id"],
                device_id=config["device_id"],
                access_token=config["access_token"],
            )
    return client
