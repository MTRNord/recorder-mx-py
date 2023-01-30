# SPDX-FileCopyrightText: 2023-present MTRNord <support@nordgedanken.dev>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
from dataclasses import dataclass
from fractions import Fraction
import os
import random
import string
from typing import Dict, List, Optional, Tuple, Union
import sys
import time
from logbook import Logger, StreamHandler
import logbook
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
    ToDeviceCallAnswerEvent,
    # ToDeviceCallNegotiateEvent,
    # CallNegotiateEvent,
)
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack,
    RTCIceGatherer,
    RTCIceCandidate,
)
from aiortc.contrib.media import MediaRecorder, MediaPlayer, MediaStreamError, Frame
from aiortc.rtcicetransport import candidate_from_aioice, candidate_to_aioice
from aioice.candidate import Candidate
import av
from av.filter import Filter, Graph

# import logging

# logging.basicConfig(level=logging.INFO)
# logging.getLogger("libav").setLevel(logging.DEBUG)


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
    prepare_waiter: Optional[asyncio.Future]
    candidate_waiter: Optional[asyncio.Future]
    room_id: Optional[str]


@dataclass
class Others:
    user_id: str
    device_id: str
    session_id: str


class ProxyTrack(MediaStreamTrack):
    _source: MediaStreamTrack
    _source_wait: Optional[asyncio.Future]
    _graph: Optional[Graph]

    def __init__(
        self,
        source: MediaPlayer,
    ) -> None:
        super().__init__()
        self.kind = source.video.kind
        self._source = source.video
        self._graph = None

    async def start(self, frame: Frame) -> None:
        self._graph = Graph()
        graph_source = self._graph.add_buffer(template=frame)

        graph_filter = self._graph.add(
            "drawtext",
            r"text='Recording Duration: %{pts:gmtime:0:%H\:%M\:%S}':x=(w-text_w)/2:y=(h-text_h)/2:fontcolor=white:fontsize=128",
        )
        graph_sink = self._graph.add("buffersink")

        graph_source.link_to(graph_filter, 0, 0)
        graph_filter.link_to(graph_sink, 0, 0)
        self._graph.configure()

    async def recv(self) -> Frame:
        try:
            frame = await self._source.recv()
            if self._graph is None:
                await self.start(frame)
            self._graph.push(frame)
            filtered_frame = self._graph.pull()
            return filtered_frame
        except MediaStreamError as e:
            frame = await self._source.recv()
            logger.warning(f"Error in recv: {e}")
            return frame
        except Exception as e:
            logger.warning(f"Error in recv: {e}")
            return await self._source.recv()

    def stop(self) -> None:
        self._source.stop()
        super().stop()


class Recorder:
    __conns: Dict[UniqueCallID, WrappedConn]
    party_id: str
    client: AsyncClient
    loop: asyncio.AbstractEventLoop
    conf_room: Dict[ConfID, MatrixRoom]
    recording_rooms: List[ConfID]
    room_conf: Dict[RoomID, ConfID]
    others: Dict[ConfID, List[Others]]
    output_track: ProxyTrack

    def __init__(self, client) -> None:
        self.client = client
        self.loop = asyncio.get_event_loop()

    async def start(self) -> None:
        self.__conns = {}
        self.conf_room = {}
        self.room_conf = {}
        self.others = {}
        self.output_track = ProxyTrack(
            MediaPlayer(
                "./test.png",
                options={
                    "loop": "1",
                    "framerate": "1",
                },
            )
        )

        self.recording_rooms = list()
        self.party_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=8)
        )

        if not os.path.exists(RECORDING_PATH):
            os.makedirs(RECORDING_PATH)

        logger.info("Starting recording handler")

    async def stop(self) -> None:
        logger.info("Stopping recording handler")
        for (_, conf_or_call_id), conn in self.__conns.items():
            await self.hangup(conf_or_call_id, conn)

    def add_call(self, conf_id: ConfID, room: MatrixRoom) -> None:
        logger.info(f"Adding conf {conf_id} to room {room.room_id}")
        self.conf_room[conf_id] = room
        self.room_conf[room.room_id] = conf_id

    async def hangup(self, conf_or_call_id: ConfID, conn: WrappedConn) -> None:
        hangup = {
            "call_id": conf_or_call_id,
            "version": "1",
            "party_id": self.party_id,
            "conf_id": conf_or_call_id,
        }
        if conn.room_id:
            # We are lazy and send it to the room and as to_device message
            await self.client.room_send(
                conn.room_id,
                "m.call.hangup",
                hangup,
                ignore_unverified_devices=True,
            )
        if conf_or_call_id in self.others:
            # Send it as to_device message
            others = self.others[conf_or_call_id]

            for data in others:
                if data.user_id == self.client.user_id:
                    continue

                message = ToDeviceMessage(
                    "m.call.hangup",
                    data.user_id,
                    data.device_id,
                    hangup,
                )
                logger.info("Sending hangup")
                await self.client.to_device(message)
        await self.client.room_put_state(
            self.conf_room[conf_or_call_id].room_id,
            "org.matrix.msc3401.call.member",
            {"m.calls": []},
            state_key=self.client.user_id,
        )

        await conn.pc.close()

    async def leave_call(self, room: MatrixRoom) -> None:
        if room.room_id in self.room_conf:
            conf_id = self.room_conf[room.room_id]
            for (user_id, conf_or_call_id), conn in list(self.__conns.items()):
                if conf_or_call_id == conf_id:
                    await self.hangup(conf_or_call_id, conn)
                    del self.__conns[(user_id, conf_or_call_id)]

            if conf_id in self.conf_room:
                del self.conf_room[conf_id]
            del self.room_conf[room.room_id]
            self.output_track = ProxyTrack(
                MediaPlayer(
                    "./test.png",
                    options={
                        "loop": "1",
                        "framerate": "1",
                    },
                )
            )

            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.notice",
                    "body": "Recording stopped",
                },
                ignore_unverified_devices=True,
            )

    async def remove_connection(self, room: MatrixRoom) -> None:
        if room.room_id in self.room_conf:
            call_id = self.room_conf[room.room_id]
            if (self.client.user_id, call_id) in self.__conns:
                del self.__conns[(self.client.user_id, call_id)]
                del self.room_conf[room.room_id]
                del self.conf_room[call_id]

    def track_others(
        self, conf_id: str, device_id: str, user_id: str, session_id: str
    ) -> None:
        if conf_id not in self.others:
            self.others[conf_id] = list()
        self.others[conf_id].append(Others(user_id, device_id, session_id))

    def remove_other(self, user_id: str):
        for other in self.others.values():
            other[:] = [o for o in other if o.user_id != user_id]

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
            call_state = next(
                (
                    event
                    for event in room_state.events
                    if event["type"] == "org.matrix.msc3401.call"
                ),
                None,
            )
            if call_state is None:
                logger.warning(f"No call state event in {room.room_id}")
                return

            member_states = [
                event
                for event in room_state.events
                if event["type"] == "org.matrix.msc3401.call.member"
            ]

            call_id = call_state["state_key"]
            logger.info(f"Found call id {call_id} for {room.room_id}")
            self.add_call(call_id, room)
            for member in member_states:
                for call in member["content"]["m.calls"]:
                    for device in call["m.devices"]:
                        self.track_others(
                            call["m.call_id"],
                            device["device_id"],
                            member["sender"],
                            device["session_id"],
                        )

        logger.info(f"Joining call in {room.room_id}")
        await self.client.room_put_state(
            room.room_id,
            "org.matrix.msc3401.call.member",
            {"m.calls": []},
            state_key=self.client.user_id,
        )

        # Send offer to others
        conf_id = self.room_conf[room.room_id]
        logger.info(f"Sending offer to others: {conf_id}")
        if self.room_conf[room.room_id] in self.others:
            # Send it as to_device message
            others = self.others[conf_id]
            logger.info(f"Sending offers to {others}")

            call_id = "".join(random.choices(string.ascii_letters + string.digits, k=8))
            for data in others:
                if data.user_id == self.client.user_id:
                    continue

                logger.info(f"Making offer for {data.user_id}")
                # Create offer
                pc = RTCPeerConnection()
                logger.info(f"Started ice for {call_id}")
                unique_id = (data.user_id, self.room_conf[room.room_id])
                conn = self.__conns[unique_id] = WrappedConn(
                    pc=pc,
                    candidate_waiter=None,
                    prepare_waiter=None,
                    room_id=room.room_id if room else None,
                )
                logger.info(f"Created connection {unique_id}")

                # conn.pc.addTransceiver("video", direction="recvonly")
                # conn.pc.addTransceiver("audio", direction="recvonly")

                conn.pc.addTrack(self.output_track)

                offer = await conn.pc.createOffer()
                await conn.pc.setLocalDescription(offer)

                logger.info(f"Got local candidates for {call_id}")
                candidates: List[Tuple[RTCIceCandidate, str]] = []
                for transceiver in conn.pc.getTransceivers():
                    gatherer: RTCIceGatherer = (
                        transceiver.sender.transport.transport.iceGatherer
                    )
                    # await gatherer.gather()
                    for candidate in gatherer.getLocalCandidates():
                        candidate.sdpMid = transceiver.mid
                        candidates.append(
                            (
                                candidate,
                                str(gatherer.getLocalParameters().usernameFragment),
                            )
                        )

                # @conn.pc.on("connectionstatechange")
                # async def on_connectionstatechange() -> None:
                #    if conn.pc.connectionState == "failed":
                #        logger.info(f"Connection {unique_id} failed")
                #        await conn.pc.close()
                #        self.__conns.pop(unique_id, None)

                offer_message = ToDeviceMessage(
                    "m.call.invite",
                    recipient=data.user_id,
                    recipient_device=data.device_id,
                    content={
                        "lifetime": 60000,
                        "invitee": data.user_id,
                        "offer": {
                            "sdp": conn.pc.localDescription.sdp,
                            "type": conn.pc.localDescription.type,
                        },
                        "version": "1",
                        "conf_id": conf_id,
                        "call_id": call_id,
                        "party_id": self.party_id,
                        "seq": 0,
                        "device_id": self.client.device_id,
                        "sender_session_id": f"{self.client.user_id}_{self.client.device_id}_session",
                        "dest_session_id": data.session_id,
                        "capabilities": {
                            "m.call.transferee": False,
                            "m.call.dtmf": False,
                        },
                    },
                )
                await self.client.to_device(offer_message)

                logger.info(f"Sending candidates to {data.user_id} {data.device_id}")
                candidates_message = ToDeviceMessage(
                    "m.call.candidates",
                    recipient=data.user_id,
                    recipient_device=data.device_id,
                    content={
                        "candidates": [
                            {
                                "candidate": f"candidate:{candidate_to_aioice(c).to_sdp()}",
                                "sdpMid": c.sdpMid,
                                # "sdpMLineIndex": c.sdpMLineIndex,
                                "usernameFragment": usernameFragment,
                            }
                            for (c, usernameFragment) in candidates
                        ],
                        "call_id": call_id,
                        "party_id": self.party_id,
                        "version": "1",
                        "seq": 1,
                        "conf_id": conf_id,
                        "device_id": self.client.device_id,
                        "sender_session_id": f"{self.client.user_id}_{self.client.device_id}_session",
                        "dest_session_id": data.session_id,
                    },
                )
                await self.client.to_device(candidates_message)
                await asyncio.sleep(0)

    # async def handle_negotiation(
    #    self,
    #    room: Optional[MatrixRoom],
    #    event: [ToDeviceCallNegotiateEvent, CallNegotiateEvent],
    # ):
    #    pass

    async def handle_call_answer(self, event: ToDeviceCallAnswerEvent):
        logger.info(f"Received call answer from {event.sender}")

        unique_id: UniqueCallID = (event.sender, event.conf_id)
        logger.info(f"Received call answer for {unique_id}")

        if event.conf_id not in self.conf_room:
            logger.warning("Got invalid to-device call answer")
            return

        while unique_id not in self.__conns:
            logger.debug("Waiting for connection of answer to be created")
            await asyncio.sleep(0.2)

        try:
            conn = self.__conns[unique_id]
        except KeyError:
            logger.warning("Received answer for unknown call")
            return

        logger.info(f"Setting remote description for {unique_id}")
        await conn.pc.setRemoteDescription(
            RTCSessionDescription(
                sdp=str(event.answer.get("sdp")), type=str(event.answer.get("type"))
            )
        )

        others = self.others[event.conf_id]
        data = next((x for x in others if x.user_id == event.sender), None)

        if not data:
            logger.warning("Received answer for unknown call")
            return

        message = ToDeviceMessage(
            "m.call.select_answer",
            recipient=event.sender,
            recipient_device=event.source["content"]["device_id"],
            content={
                "selected_party_id": event.party_id,
                "call_id": event.call_id,
                "party_id": self.party_id,
                "version": "1",
                "seq": 2,
                "conf_id": event.conf_id,
                "device_id": self.client.device_id,
                "sender_session_id": f"{self.client.user_id}_{self.client.device_id}_session",
                "dest_session_id": data.session_id,
            },
        )
        await self.client.to_device(message)

        await self.client.room_put_state(
            self.conf_room[event.conf_id].room_id,
            "org.matrix.msc3401.call.member",
            {
                "m.calls": [
                    {
                        "m.call_id": event.conf_id,
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
        conn = self.__conns[unique_id] = WrappedConn(
            pc=pc,
            candidate_waiter=self.loop.create_future(),
            prepare_waiter=self.loop.create_future(),
            room_id=room.room_id if room else None,
        )
        logger.info("Adding tracks")
        input_tracks = {}

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
                self.__conns.pop(unique_id, None)
            if pc.connectionState == "connected":
                asyncio.create_task(task())

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            input_tracks[track.kind] = track

        logger.info("Waiting for prepare")
        await pc.setRemoteDescription(offer)

        logger.info("Ready to receive candidates")
        if conn.prepare_waiter:
            conn.prepare_waiter.set_result(None)

        if room and isinstance(event, CallInviteEvent):
            logger.info("Sending receipt")
            await self.client.update_receipt_marker(room.room_id, event.event_id)

        if conn.candidate_waiter:
            await conn.candidate_waiter
        logger.info("Got candidates")

        logger.info("Creating answer")
        answer = await pc.createAnswer()
        if not answer:
            logger.warning("Failed to create answer")
            await pc.close()
            self.__conns.pop(unique_id, None)
            if room:
                await self.hangup(event.call_id, conn)
            elif isinstance(event, ToDeviceCallInviteEvent):
                await self.hangup(event.conf_id, conn)
            return
        await pc.setLocalDescription(answer)

        logger.info("Sending answer")
        if room:
            answer = {
                "call_id": event.call_id,
                "version": "1",
                "party_id": self.party_id,
                "answer": {
                    "type": pc.localDescription.type,
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
                    "type": pc.localDescription.type,
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
        while unique_id not in self.__conns:
            logger.debug("Waiting for connection to be created")
            await asyncio.sleep(0.2)

        try:
            conn = self.__conns[unique_id]
        except KeyError:
            logger.warning("Received candidates for unknown call")
            return

        logger.info("Waiting for prepare")
        if conn.prepare_waiter:
            await conn.prepare_waiter
        logger.info("Adding candidates")
        for raw_candidate in event.candidates:
            if not raw_candidate.get("candidate"):
                # End of candidates
                try:
                    if conn.candidate_waiter:
                        conn.candidate_waiter.set_result(None)
                except asyncio.InvalidStateError:
                    pass
                break
            try:
                candidate = candidate_from_aioice(
                    Candidate.from_sdp(raw_candidate.get("candidate"))
                )
            except ValueError as error:
                logger.warning(f"Received invalid candidate: {error}")
                continue
            candidate.sdpMid = raw_candidate.get("sdpMid")
            candidate.sdpMLineIndex = raw_candidate.get("sdpMLineIndex")

            logger.info(f"Adding candidate {candidate} for {event.call_id}")
            await conn.pc.addIceCandidate(candidate)
        logger.info("Done adding candidates")
        try:
            if conn.candidate_waiter:
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
        reason = None
        if "reason" in event.source["content"]:
            reason = event.source["content"]["reason"]
        if room:
            logger.info(
                f"Received call hangup from {event.sender} in {room.room_id} with reason {reason}"
            )
        else:
            logger.info(
                f"Received call hangup from {event.sender} with reason {reason}"
            )
        if event.sender == self.client.user_id:
            return

        # TODO: This is incorrect:
        if reason == "replaced":
            logger.warning("Call was replaced but we ignore that for now")
            return
        try:
            if isinstance(event, CallHangupEvent):
                unique_id: UniqueCallID = (event.sender, event.call_id)
            else:
                unique_id: UniqueCallID = (event.sender, event.conf_id)
            await self.__conns.pop(unique_id).pc.close()
            if room and isinstance(event, CallHangupEvent):
                await self.client.update_receipt_marker(room.room_id, event.event_id)

        except KeyError:
            logger.warning("Received hangup for unknown call")
            return
