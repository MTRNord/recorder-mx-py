# SPDX-FileCopyrightText: 2023-present MTRNord <support@nordgedanken.dev>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import getpass
import json
import os
import random
import string
import sys
import time
from typing import Dict, Optional, Tuple
from attr import dataclass
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaRecorder, MediaBlackhole
from aiortc.rtcicetransport import candidate_from_aioice
from aioice.candidate import Candidate
from nio import (
    AsyncClient,
    LoginResponse,
    MatrixRoom,
    CallInviteEvent,
    CallCandidatesEvent,
    CallHangupEvent,
    ToDeviceCallInviteEvent,
    ToDeviceCallCandidatesEvent,
    ToDeviceCallHangupEvent,
    AsyncClientConfig,
    InviteEvent,
    MSC3401CallEvent,
    ToDeviceMessage,
    to_device,
    CallMemberEvent,
)

from logbook import Logger, StreamHandler
import logbook
import sys

StreamHandler(sys.stdout).push_application()
logger = Logger(__name__)
logger.level = logbook.INFO
# crypto.logger.level = logbook.DEBUG
to_device.logger.level = logbook.DEBUG


RoomID = str
UserID = str
UniqueCallID = Tuple[RoomID, UserID, str]

# file to store credentials in case you want to run program multiple times
CONFIG_FILE = "credentials.json"  # login credentials JSON file
# directory to store persistent data for end-to-end encryption
STORE_PATH = "./store/"  # local directory
# directory to store recordings
RECORDING_PATH = "./recordings/"  # local directory


@dataclass
class WrappedConn:
    pc: RTCPeerConnection
    prepare_waiter: asyncio.Future
    candidate_waiter: asyncio.Future


class RecordingBot:
    conns: Dict[UniqueCallID, WrappedConn]
    party_id: str
    client: AsyncClient
    loop: asyncio.AbstractEventLoop

    def __init__(self, client) -> None:
        self.client = client
        self.loop = asyncio.get_event_loop()

    async def start(self) -> None:
        self.conns = {}
        self.party_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=8)
        )

        if not os.path.exists(RECORDING_PATH):
            os.makedirs(RECORDING_PATH)

        logger.info("Starting client")

        if self.client.should_upload_keys:
            await self.client.keys_upload()
            logger.debug("Uploaded keys")

        self.client.add_event_callback(self.call_candidates, CallCandidatesEvent)
        self.client.add_event_callback(self.call_invite, CallInviteEvent)
        self.client.add_event_callback(self.call_hangup, CallHangupEvent)
        self.client.add_event_callback(self.msc3401_call, MSC3401CallEvent)
        self.client.add_event_callback(self.msc3401_call_member, CallMemberEvent)

        self.client.add_to_device_callback(
            self.to_device_call_candidates, (ToDeviceCallCandidatesEvent,)
        )
        self.client.add_to_device_callback(
            self.to_device_call_invite, (ToDeviceCallInviteEvent,)
        )
        self.client.add_to_device_callback(
            self.to_device_call_hangup, (ToDeviceCallHangupEvent,)
        )

        self.client.add_event_callback(self.cb_autojoin_room, InviteEvent)
        logger.info("Listening for calls")

        await self.client.sync_forever(
            timeout=30000, full_state=True, set_presence="online"
        )

    async def stop(self) -> None:
        logger.info("Stopping client")
        for (room_id, _, call_id), conn in self.conns.items():
            if room_id.startswith("!"):
                hangup = {
                    "content": {
                        "call_id": call_id,
                        "version": 1,
                        "party_id": self.party_id,
                    }
                }
                await self.client.room_send(
                    room_id, "m.call.hangup", hangup, ignore_unverified_devices=True
                )
                await self.client.room_put_state(
                    room_id,
                    "org.matrix.msc3401.call.member",
                    {
                        "m.calls": [
                            {
                                "m.call_id": call_id,
                                "m.devices": [],
                            }
                        ]
                    },
                    state_key=self.client.user_id,
                )
            await conn.pc.close()
        await self.client.close()
        pass

    # TODO: Negotiate new streams
    # TODO: Handle m.call.negotiate to-device events
    # TODO: Send m.call.negotiate events with type offer
    async def msc3401_call_member(
        self, room: MatrixRoom, event: CallMemberEvent
    ) -> None:
        """Handles incoming MSC3401 call members."""
        logger.info(f"MSC3401 call member event: {event}")

        for call in event.calls:
            # TODO: Can I reuse the same connection? Do I have the info needed? Is it a new connection?
            pass
            # asyncio.create_task(self.handle_call_invite(event, room))

    async def msc3401_call(self, room: MatrixRoom, event: MSC3401CallEvent) -> None:
        """Handles incoming MSC3401 calls."""
        await self.client.room_put_state(
            room.room_id,
            "org.matrix.msc3401.call.member",
            {
                "m.calls": [
                    {
                        "m.call_id": event.state_key,
                        "m.devices": [
                            {
                                "device_id": self.client.device_id,
                                "expires_ts": int(time.time() * 1000)
                                + (1000 * 60 * 60),
                                "session_id": f"{self.client.user_id}_{self.client.device_id}_session",
                                "feeds": [
                                    {
                                        "purpose": "m.usermedia",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
            state_key=self.client.user_id,
        )

    async def handle_call_invite(
        self, event: CallInviteEvent, room: Optional[MatrixRoom]
    ) -> None:
        if room:
            logger.info(f"Received call invite from {event.sender} in {room.room_id}")
        else:
            logger.info(f"Received call invite from {event.sender}")
        if event.expired:
            logger.warning("Call invite expired")
            return
        if event.version != "1":
            logger.warning("Call invite version not supported")
            return

        logger.info("Preparing call")
        offer = RTCSessionDescription(
            sdp=event.offer.get("sdp"), type=str(event.offer.get("type"))
        )
        pc = RTCPeerConnection()
        if room:
            unique_id = (room.room_id, event.sender, event.call_id)
        else:
            unique_id = (
                event.source["content"]["conf_id"],
                event.sender,
                event.call_id,
            )
        conn = self.conns[unique_id] = WrappedConn(
            pc=pc,
            candidate_waiter=self.loop.create_future(),
            prepare_waiter=self.loop.create_future(),
        )
        logger.info("Adding tracks")
        input_tracks = {}
        # output_tracks = {"audio": MediaBlackhole(), "video": MediaBlackhole()}

        async def task() -> None:
            base_name_audio = f"{event.sender}_{event.call_id}"
            base_name_video = f"{event.sender}_{event.call_id}"
            if os.path.exists(os.path.join(RECORDING_PATH, f"{base_name_audio}.wav")):
                i = 1
                while os.path.exists(
                    os.path.join(RECORDING_PATH, f"{base_name_audio}_{i}.wav")
                ):
                    i += 1
                base_name_audio = f"{base_name_audio}_{i}"

            if os.path.exists(os.path.join(RECORDING_PATH, f"{base_name_video}.mp4")):
                i = 1
                while os.path.exists(
                    os.path.join(RECORDING_PATH, f"{base_name_video}_{i}.mp4")
                ):
                    i += 1
                base_name_video = f"{base_name_video}_{i}"

            wav_file = os.path.join(RECORDING_PATH, f"{base_name_audio}.wav")
            mp4_file = os.path.join(RECORDING_PATH, f"{base_name_video}.mp4")
            audio_recorder = MediaRecorder(wav_file, format="wav")
            audio_recorder.addTrack(input_tracks["audio"])
            await audio_recorder.start()
            if "video" in input_tracks:
                video_recorder = MediaRecorder(mp4_file, format="mp4")
                video_recorder.addTrack(input_tracks["video"])
                await video_recorder.start()
            else:
                video_recorder = None

            audio_track = input_tracks["audio"]
            if "video" in input_tracks:
                video_track = input_tracks["video"]

                @video_track.on("ended")
                async def on_ended_video():
                    if video_recorder:
                        await video_recorder.stop()

            @audio_track.on("ended")
            async def on_ended_audio():
                await audio_recorder.stop()

        logger.info("Setting up callbacks")

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            if pc.connectionState == "failed":
                await pc.close()
                self.conns.pop(unique_id, None)
            if pc.connectionState == "connected":
                asyncio.create_task(task())

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            input_tracks[track.kind] = track

            # pc.addTrack(output_tracks[track.kind])

        logger.info("Waiting for prepare")
        await pc.setRemoteDescription(offer)
        conn.prepare_waiter.set_result(None)

        if room:
            logger.info("Sending receipt")
            await self.client.update_receipt_marker(room.room_id, event.event_id)

        logger.info("Waiting for candidates")
        await conn.candidate_waiter

        logger.info("Creating answer")
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        logger.info("Sending answer")
        if room:
            answer = {
                "call_id": event.call_id,
                "version": "1",
                "party_id": self.party_id,
                "answer": {
                    # "type": pc.localDescription.type,
                    "type": "answer",
                    "sdp": pc.localDescription.sdp,
                },
            }
            await self.client.room_send(
                room.room_id, "m.call.answer", answer, ignore_unverified_devices=True
            )
        else:
            answer = {
                "call_id": event.call_id,
                "version": "1",
                "party_id": self.party_id,
                "conf_id": event.source["content"]["conf_id"],
                "capabilities": {
                    "m.call.transferee": False,
                    "m.call.dtmf": False,
                },
                "answer": {
                    # "type": pc.localDescription.type,
                    "type": "answer",
                    "sdp": pc.localDescription.sdp,
                },
                "device_id": self.client.device_id,
                "dest_session_id": event.source["content"]["sender_session_id"],
                "sender_session_id": f"{self.client.user_id}_{self.client.device_id}_session",
                "seq": event.source["content"]["seq"],
            }
            to_device_message = ToDeviceMessage(
                "m.call.answer",
                event.source["sender"],
                event.source["sender_device"],
                answer,
            )
            await self.client.to_device(to_device_message)
        if room:
            logger.info(f"Sent answer to {event.sender} in {room.room_id}")
        else:
            logger.info(
                f"Sent answer to {event.sender} with device {event.source['sender_device']}"
            )

    async def call_invite(self, room: MatrixRoom, event: CallInviteEvent) -> None:
        """Handles incoming call invites."""

        asyncio.create_task(self.handle_call_invite(event, room))

    async def to_device_call_invite(self, event: CallInviteEvent) -> None:
        """Handles incoming call invites."""
        logger.info("Received to-device call invite")

        asyncio.create_task(self.handle_call_invite(event, None))

    async def handle_call_candidates(
        self, room: Optional[MatrixRoom], event: CallCandidatesEvent
    ) -> None:
        logger.info("Delaying candidates for 3 seconds")
        await asyncio.sleep(3)
        if room:
            logger.info(
                f"Received call candidates from {event.sender} in {room.room_id}"
            )
        else:
            logger.info(f"Received call candidates from {event.sender}")

        if room:
            unique_id = (room.room_id, event.sender, event.call_id)
        else:
            unique_id = (
                event.source["content"]["conf_id"],
                event.sender,
                event.call_id,
            )
        try:
            conn = self.conns[unique_id]
        except KeyError as e:
            logger.warning("Received candidates for unknown call")
            return
        logger.info("Waiting for prepare")
        await conn.prepare_waiter
        logger.info("Adding candidates")
        for raw_candidate in event.candidates:
            if not raw_candidate.get("candidate"):
                # End of candidates
                conn.candidate_waiter.set_result(None)
                break
            try:
                candidate = candidate_from_aioice(
                    Candidate.from_sdp(raw_candidate.get("candidate"))
                )
            except ValueError as e:
                logger.warning("Received invalid candidate: %s", e)
                continue
            candidate.sdpMid = raw_candidate.get("sdpMid")
            candidate.sdpMLineIndex = raw_candidate.get("sdpMLineIndex")

            logger.info(f"Adding candidate {candidate} for {event.call_id}")
            await conn.pc.addIceCandidate(candidate)
        logger.info("Done adding candidates")
        try:
            conn.candidate_waiter.set_result(None)
        except asyncio.InvalidStateError:
            pass
        if room:
            await self.client.update_receipt_marker(room.room_id, event.event_id)

    async def call_candidates(
        self, room: MatrixRoom, event: CallCandidatesEvent
    ) -> None:
        """Handles incoming call candidates."""

        asyncio.create_task(self.handle_call_candidates(room, event))

    async def to_device_call_candidates(self, event: CallCandidatesEvent) -> None:
        """Handles incoming call candidates."""
        logger.info("Received to-device call candidates")

        asyncio.create_task(self.handle_call_candidates(None, event))

    async def handle_call_hangup(
        self,
        room: Optional[MatrixRoom],
        event: CallHangupEvent,
    ) -> None:
        if room:
            logger.info(f"Received call hangup from {event.sender} in {room.room_id}")
        try:
            if room:
                await self.conns.pop(
                    (room.room_id, event.sender, event.call_id)
                ).pc.close()
                await self.client.update_receipt_marker(room.room_id, event.event_id)
            else:
                await self.conns.pop(
                    (
                        event.source["content"]["conf_id"],
                        event.sender,
                        event.call_id,
                    )
                ).pc.close()

        except KeyError:
            logger.warning("Received hangup for unknown call")
            return

    async def call_hangup(self, room: MatrixRoom, event: CallHangupEvent) -> None:
        """Handles call hangups."""
        asyncio.create_task(self.handle_call_hangup(room, event))

    async def to_device_call_hangup(self, event: CallHangupEvent) -> None:
        """Handles call hangups."""
        asyncio.create_task(self.handle_call_hangup(None, event))

    async def cb_autojoin_room(self, room: MatrixRoom, event: InviteEvent):
        """Callback to automatically joins a Matrix room on invite.

        Arguments:
            room {MatrixRoom} -- Provided by nio
            event {InviteEvent} -- Provided by nio
        """
        asyncio.create_task(self.client.join(room.room_id))
        logger.info(f"Joining room {room.room_id} on invite from {event.sender}")


def write_details_to_disk(resp: LoginResponse, homeserver) -> None:
    """Writes the required login details to disk so we can log in later without
    using a password.

    Arguments:
        resp {LoginResponse} -- the successful client login response.
        homeserver -- URL of homeserver, e.g. "https://matrix.example.org"
    """
    # open the config file in write-mode
    with open(CONFIG_FILE, "w") as f:
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
        with open(CONFIG_FILE, "r") as f:
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


bot = None


async def main() -> None:
    logger.info("Starting bot...")
    client = await login()
    global bot
    bot = RecordingBot(client=client)

    await bot.start()


if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except Exception:
        if bot:
            asyncio.get_event_loop().run_until_complete(bot.stop())
        logger.exception("Fatal Runtime error.")
        sys.exit(1)
    except KeyboardInterrupt:
        if bot:
            asyncio.get_event_loop().run_until_complete(bot.stop())
        print("Received keyboard interrupt.")
        sys.exit(0)
