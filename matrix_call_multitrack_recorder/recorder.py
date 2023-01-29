# SPDX-FileCopyrightText: 2023-present MTRNord <support@nordgedanken.dev>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
from dataclasses import dataclass
import os
import random
import string
from typing import Dict, List, Optional, Tuple, Union
from logbook import Logger, StreamHandler
import logbook
import sys
from nio import (
    AsyncClient,
    CallInviteEvent,
    MatrixRoom,
    ToDeviceMessage,
    CallCandidatesEvent,
    RoomGetStateError,
    ToDeviceCallCandidatesEvent,
    CallHangupEvent,
    ToDeviceCallInviteEvent,
    ToDeviceCallHangupEvent,
)
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaRecorder, MediaBlackhole
from aiortc.rtcicetransport import candidate_from_aioice
from aioice.candidate import Candidate
import time

StreamHandler(sys.stdout).push_application()
logger = Logger(__name__)
logger.level = logbook.INFO

# directory to store recordings
RECORDING_PATH = "./recordings/"  # local directory

UserID = str
ConfID = str
RoomID = str
UniqueCallID = Tuple[UserID, ConfID]


@dataclass
class WrappedConn:
    pc: RTCPeerConnection
    prepare_waiter: asyncio.Future
    candidate_waiter: asyncio.Future
    room_id: Optional[str]


@dataclass
class Others:
    user_id: str
    device_id: str


class Recorder:
    conns: Dict[UniqueCallID, WrappedConn]
    party_id: str
    client: AsyncClient
    loop: asyncio.AbstractEventLoop
    conf_room: Dict[ConfID, MatrixRoom]
    recording_rooms: List[ConfID]
    room_conf: Dict[RoomID, ConfID]
    others: Dict[ConfID, List[Others]]

    def __init__(self, client) -> None:
        self.client = client
        self.loop = asyncio.get_event_loop()

    async def start(self) -> None:
        self.conns = {}
        self.conf_room = {}
        self.room_conf = {}
        self.others = {}
        self.recording_rooms = list()
        self.party_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=8)
        )

        if not os.path.exists(RECORDING_PATH):
            os.makedirs(RECORDING_PATH)

        logger.info("Starting recording handler")

    async def stop(self) -> None:
        logger.info("Stopping recording handler")
        for (_, conf_or_call_id), conn in self.conns.items():

            if conn.room_id:
                hangup = {
                    "content": {
                        "call_id": conf_or_call_id,
                        "version": 1,
                        "party_id": self.party_id,
                    }
                }
                # We are lazy and send it to the room and as to_device message
                await self.client.room_send(
                    conn.room_id,
                    "m.call.hangup",
                    hangup,
                    ignore_unverified_devices=True,
                )
            else:
                hangup = {
                    "content": {
                        "call_id": conf_or_call_id,
                        "version": "1",
                        "party_id": self.party_id,
                    }
                }
                if conf_or_call_id in self.others:
                    # Send it as to_device message
                    others = self.others[conf_or_call_id]

                    for data in others:
                        message = ToDeviceMessage(
                            "m.call.hangup",
                            data.user_id,
                            data.device_id,
                            hangup,
                        )
                        await self.client.to_device(message)
                await self.client.room_put_state(
                    self.conf_room[conf_or_call_id].room_id,
                    "org.matrix.msc3401.call.member",
                    {
                        "m.calls": [
                            {
                                "m.call_id": conf_or_call_id,
                                "m.devices": [],
                            }
                        ]
                    },
                    state_key=self.client.user_id,
                )
            await conn.pc.close()
        pass

    def add_call(self, conf_id: ConfID, room: MatrixRoom) -> None:
        logger.info(f"Adding conf {conf_id} to room {room.room_id}")
        self.conf_room[conf_id] = room
        self.room_conf[room.room_id] = conf_id

    async def leave_call(self, room: MatrixRoom) -> None:
        if room.room_id in self.room_conf:
            conf_id = self.room_conf[room.room_id]
            if conf_id in self.conf_room:
                del self.conf_room[conf_id]
            del self.room_conf[room.room_id]

            for (user_id, conf_or_call_id), conn in list(self.conns.items()):
                if conf_or_call_id in self.others:
                    others = self.others[conf_or_call_id]
                    for data in others:
                        hangup_message = ToDeviceMessage(
                            "m.call.hangup",
                            data.user_id,
                            data.device_id,
                            {
                                "call_id": conf_id,
                                "version": "1",
                                "party_id": self.party_id,
                            },
                        )
                        logger.info(f"Sending hangup {hangup_message}")
                        await self.client.to_device(hangup_message)

                await conn.pc.close()
                del self.conns[(user_id, conf_or_call_id)]
            await self.client.room_put_state(
                room.room_id,
                "org.matrix.msc3401.call.member",
                {
                    "m.calls": [
                        {
                            "m.call_id": conf_id,
                            "m.devices": [],
                        }
                    ]
                },
                state_key=self.client.user_id,
            )

    async def remove_connection(self, room: MatrixRoom) -> None:
        if room.room_id in self.room_conf:
            call_id = self.room_conf[room.room_id]
            if (self.client.user_id, call_id) in self.conns:
                del self.conns[(self.client.user_id, call_id)]
                del self.room_conf[room.room_id]
                del self.conf_room[call_id]

    def track_others(self, conf_id: str, device_id: str, user_id: str) -> None:
        if conf_id not in self.others:
            self.others[conf_id] = list()
        self.others[conf_id].append(Others(user_id, device_id))

    async def join_call(self, room: MatrixRoom) -> None:
        if room.room_id not in self.room_conf:
            logger.warning(
                f"Room {room.room_id} is not a call room or we forgot. Trying to get call id from state"
            )
            room_state = await self.client.room_get_state(room.room_id)
            if isinstance(room_state, RoomGetStateError):
                logger.warning(f"Could not get state for {room.room_id}: {room_state}")
                return

            # Find "org.matrix.msc3401.call" state event
            state = next(
                (
                    event
                    for event in room_state.events
                    if event["type"] == "org.matrix.msc3401.call"
                ),
                None,
            )
            if state is None:
                logger.warning(f"No call state event in {room.room_id}")
                return
            call_id = state["state_key"]
            logger.info(f"Found call id {call_id} for {room.room_id}")
            self.add_call(call_id, room)
            await self.client.room_put_state(
                room.room_id,
                "org.matrix.msc3401.call.member",
                {
                    "m.calls": [
                        {
                            "m.call_id": self.room_conf[room.room_id],
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
        self,
        event: Union[CallInviteEvent, ToDeviceCallInviteEvent],
        room: Optional[MatrixRoom],
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

        if isinstance(event, ToDeviceCallInviteEvent):
            if event.conf_id not in self.conf_room:
                logger.warning("Got invalid to-device call invite")
                return

        logger.info("Preparing call")
        offer = RTCSessionDescription(
            sdp=str(event.offer.get("sdp")), type=str(event.offer.get("type"))
        )
        pc = RTCPeerConnection()
        if isinstance(event, CallInviteEvent):
            unique_id: UniqueCallID = (event.sender, event.call_id)
        else:
            unique_id: UniqueCallID = (event.sender, event.conf_id)
        conn = self.conns[unique_id] = WrappedConn(
            pc=pc,
            candidate_waiter=self.loop.create_future(),
            prepare_waiter=self.loop.create_future(),
            room_id=room.room_id if room else None,
        )
        logger.info("Adding tracks")
        input_tracks = {}
        # output_tracks = {"audio": MediaBlackhole(), "video": MediaBlackhole()}

        async def task() -> None:
            if isinstance(event, CallInviteEvent):
                conf_path = RECORDING_PATH
            else:
                conf_path = os.path.join(RECORDING_PATH, f"{event.conf_id}")
            if not os.path.exists(conf_path):
                os.mkdir(conf_path)
            base_name_audio = f"{event.sender}_{event.call_id}"
            base_name_video = f"{event.sender}_{event.call_id}"
            if os.path.exists(os.path.join(conf_path, f"{base_name_audio}.wav")):
                i = 1
                while os.path.exists(
                    os.path.join(conf_path, f"{base_name_audio}_{i}.wav")
                ):
                    i += 1
                base_name_audio = f"{base_name_audio}_{i}"

            if os.path.exists(os.path.join(conf_path, f"{base_name_video}.mp4")):
                i = 1
                while os.path.exists(
                    os.path.join(conf_path, f"{base_name_video}_{i}.mp4")
                ):
                    i += 1
                base_name_video = f"{base_name_video}_{i}"

            wav_file = os.path.join(conf_path, f"{base_name_audio}.wav")
            mp4_file = os.path.join(conf_path, f"{base_name_video}.mp4")
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

        if room and isinstance(event, CallInviteEvent):
            logger.info("Sending receipt")
            await self.client.update_receipt_marker(room.room_id, event.event_id)

        logger.info("Waiting for candidates")
        await conn.candidate_waiter

        logger.info("Creating answer")
        answer = await pc.createAnswer()
        if not answer:
            logger.warning("Failed to create answer")
            await pc.close()
            self.conns.pop(unique_id, None)
            if room:
                await self.client.room_send(
                    room.room_id,
                    "m.call.hangup",
                    {
                        "call_id": event.call_id,
                        "version": "1",
                        "party_id": self.party_id,
                        "reason": "ice_failed",
                    },
                )
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.notice",
                        "body": f"Call with {event.sender} failed",
                    },
                )
            elif isinstance(event, ToDeviceCallInviteEvent):
                if unique_id not in self.conns:
                    return
                if event.conf_id not in self.others:
                    return
                else:
                    data = self.others[event.conf_id]
                    user_data: Optional[Others] = None
                    # Find user in data
                    for user in data:
                        if user.user_id == event.sender:
                            user_data = user
                            break

                    message = ToDeviceMessage(
                        type="m.call.hangup",
                        recipient=event.sender,
                        recipient_device=user_data.device_id,  # type: ignore
                        content={
                            "call_id": event.call_id,
                            "version": "1",
                            "party_id": self.party_id,
                            "reason": "ice_failed",
                        },
                    )
                    await self.client.to_device(message)
                    await self.client.room_send(
                        self.conf_room[event.conf_id].room_id,
                        "m.room.message",
                        {
                            "msgtype": "m.notice",
                            "body": f"Call with {event.sender} failed",
                        },
                    )
            return
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
            if event.call_id not in self.recording_rooms:
                self.recording_rooms.append(event.call_id)
                await self.client.room_send(
                    room.room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.notice",
                        "body": "Successfully started recording",
                    },
                    ignore_unverified_devices=True,
                )
            logger.info(f"Sent answer to {event.sender} in {room.room_id}")
        elif isinstance(event, ToDeviceCallInviteEvent):
            if event.conf_id not in self.recording_rooms:
                self.recording_rooms.append(event.conf_id)
                await self.client.room_send(
                    self.conf_room[event.conf_id].room_id,
                    "m.room.message",
                    {
                        "msgtype": "m.notice",
                        "body": "Successfully started recording",
                    },
                    ignore_unverified_devices=True,
                )
            logger.info(
                f"Sent answer to {event.sender} with device {event.source['sender_device']}"
            )

    async def handle_call_candidates(
        self,
        room: Optional[MatrixRoom],
        event: Union[CallCandidatesEvent, ToDeviceCallCandidatesEvent],
    ) -> None:
        # logger.info("Delaying candidates for 3 seconds")
        # await asyncio.sleep(3)
        if room:
            logger.info(
                f"Received call candidates from {event.sender} in {room.room_id}"
            )
        else:
            logger.info(f"Received call candidates from {event.sender}")

        if isinstance(event, CallCandidatesEvent):
            unique_id: UniqueCallID = (event.sender, event.call_id)
        else:
            unique_id: UniqueCallID = (event.sender, event.conf_id)

            if event.conf_id not in self.conf_room:
                logger.warning("Got invalid to-device call candidates")
                return
        while unique_id not in self.conns:
            logger.info("Waiting for connection to be created")
            await asyncio.sleep(0.2)

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
                try:
                    conn.candidate_waiter.set_result(None)
                except asyncio.InvalidStateError:
                    pass
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
        if room and isinstance(event, CallCandidatesEvent):
            await self.client.update_receipt_marker(room.room_id, event.event_id)

    async def handle_call_hangup(
        self,
        room: Optional[MatrixRoom],
        event: Union[CallHangupEvent, ToDeviceCallHangupEvent],
    ) -> None:
        if room:
            logger.info(f"Received call hangup from {event.sender} in {room.room_id}")
        try:
            if isinstance(event, CallHangupEvent):
                unique_id: UniqueCallID = (event.sender, event.call_id)
            else:
                unique_id: UniqueCallID = (event.sender, event.conf_id)
            await self.conns.pop(unique_id).pc.close()
            if room and isinstance(event, CallHangupEvent):
                await self.client.update_receipt_marker(room.room_id, event.event_id)

        except KeyError:
            logger.warning("Received hangup for unknown call")
            return
