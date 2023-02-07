# SPDX-FileCopyrightText: 2023-present MTRNord <support@nordgedanken.dev>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import os
import random
import string
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

# This one is actually used. Sadly we cant tell py that
import av  # type: ignore
import logbook  # type: ignore
from aioice.candidate import Candidate
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceCandidate,
    RTCIceGatherer,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.contrib.media import Frame, MediaPlayer, MediaRecorder, MediaStreamError
from aiortc.rtcicetransport import candidate_from_aioice, candidate_to_aioice

# This one is actually used. Sadly we cant tell py that
from av.filter import Filter, Graph  # type: ignore
from logbook import Logger, StreamHandler
from nio import (  # ToDeviceCallNegotiateEvent,; CallNegotiateEvent,
    AsyncClient,
    CallCandidatesEvent,
    CallHangupEvent,
    CallInviteEvent,
    MatrixRoom,
    RoomGetStateError,
    ToDeviceCallAnswerEvent,
    ToDeviceCallCandidatesEvent,
    ToDeviceCallHangupEvent,
    ToDeviceCallInviteEvent,
    ToDeviceMessage,
)

from .utils.future_map import FutureMap
from .utils.misc_types import InputTracks

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

# TODO: Make via config
#STUN = RTCConfiguration(iceServers=[RTCIceServer(urls="stun:turn.matrix.org")])


@dataclass
class Others:
    """
    Data we keep about other users in a call.
    Mostly needed for to_device messaging when setting up a call late.
    """

    user_id: str
    device_id: str
    session_id: str


class ProxyTrack(MediaStreamTrack):
    """
    Wrapper for the MediaPlayer to be able to show the duration
    """

    __source: MediaStreamTrack
    __graph: Optional[Graph]

    def __init__(
        self,
        source: MediaPlayer,
    ) -> None:
        super().__init__()
        self.kind = source.video.kind
        self.__source = source.video
        self.__graph = None

    async def start(self, frame: Frame) -> None:
        """
        This function is used to setup the filter graph that displays the
        duration of the recording.

        Note that type errors are expected since we interact with ffmpeg here.
        So graph is actually a C pointer and not a python object.
        """

        self.__graph = Graph()
        graph_source = self.__graph.add_buffer(template=frame)

        graph_filter = self.__graph.add(
            "drawtext",
            r"text='Recording Duration: %{pts:gmtime:0:%H\:%M\:%S}':x=(w-text_w)/2:y=(h-text_h)/2:fontcolor=white:fontsize=100",
        )
        graph_sink = self.__graph.add("buffersink")

        graph_source.link_to(graph_filter, 0, 0)
        graph_filter.link_to(graph_sink, 0, 0)
        self.__graph.configure()

    async def recv(self) -> Frame:
        """
        We handle the next Frame here. First we setup the graph and then use it.
        we fall back to sending the source image. (Might fail due to ffmpeg.
        It then results in a grey image.)
        """

        frame = await self.__source.recv()
        try:
            if not self.__graph:
                await self.start(frame)
            if self.__graph:
                self.__graph.push(frame)
                filtered_frame = self.__graph.pull()
                return filtered_frame
            else:
                logger.error("Video Init failed!")
            return frame
        except MediaStreamError as error:
            logger.warning(f"MediaStreamError in recv: {error}")
            return frame
        except Exception as error:
            logger.warning(f"Exception in recv: {error}")
            return frame

    def stop(self) -> None:
        self.__source.stop()
        super().stop()


@dataclass
class WrappedConn:
    """
    The state data of a WebRTC Connection we have running
    """

    pc: RTCPeerConnection
    prepare_waiter: Optional[asyncio.Future]
    candidate_waiter: Optional[asyncio.Future]
    room_id: Optional[str]
    input_tracks: InputTracks


class Recorder:
    """
    Core handling of the bot.
    This does both the webrtc handshake as well as the recording handling currently.
    """

    __conns: FutureMap[UniqueCallID, WrappedConn]
    __outputs: dict[RoomID, ProxyTrack]
    party_id: str
    client: AsyncClient
    loop: asyncio.AbstractEventLoop
    conf_room: Dict[ConfID, MatrixRoom]
    recording_rooms: List[ConfID]
    room_conf: Dict[RoomID, ConfID]
    others: Dict[ConfID, List[Others]]
    session_id: str

    def __init__(self, client) -> None:
        self.client = client
        self.loop = asyncio.get_event_loop()
        self.__conns = FutureMap()
        self.conf_room = {}
        self.room_conf = {}
        self.others = {}
        self.recording_rooms = []
        self.__outputs = {}
        self.party_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=8)
        )
        self.session_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=8)
        )

        if not os.path.exists(RECORDING_PATH):
            os.makedirs(RECORDING_PATH)

        logger.info("Starting recording handler")

    async def stop(self) -> None:
        """
        Stops and cleans up the call.
        This mainly means sending out all hangup events.
        """

        logger.info("Stopping recording handler")
        for (_, conf_or_call_id), conn in await self.__conns.items():
            await self.hangup(conf_or_call_id, conn)

    def add_call(self, conf_id: ConfID, room: MatrixRoom) -> None:
        """
        Adds the Call room to the internal state.
        """

        logger.info(f"Adding conf {conf_id} to room {room.room_id}")
        self.conf_room[conf_id] = room
        self.room_conf[room.room_id] = conf_id

    async def hangup(self, conf_or_call_id: ConfID, conn: WrappedConn) -> None:
        """
        This handles the hangup negotiation of a call.
        It also makes sure to close the WebRTC connection
        """

        hangup = {
            "call_id": conf_or_call_id,
            "version": "1",
            "party_id": self.party_id,
            "conf_id": conf_or_call_id,
        }
        if conn.room_id and conf_or_call_id not in self.others:
            # We are lazy and send it to the room and as to_device message
            await self.client.room_send(
                conn.room_id,
                "m.call.hangup",
                hangup,
                ignore_unverified_devices=True,
            )

        already_sent_to = []
        if conf_or_call_id in self.others:
            # Send it as to_device message
            others = self.others[conf_or_call_id]

            for data in others:
                if data.user_id == self.client.user_id:
                    continue
                if data in already_sent_to:
                    continue

                hangup["device_id"] = str(self.client.device_id)
                hangup["sender_session_id"] = self.session_id
                hangup["dest_session_id"] = data.session_id

                message = ToDeviceMessage(
                    "m.call.hangup",
                    data.user_id,
                    data.device_id,
                    hangup,
                )
                logger.info("Sending hangup via to_device")
                await self.client.to_device(message)
                already_sent_to.append(data)

        # If as this might be not the case due to races
        if conf_or_call_id in self.conf_room:
            await self.client.room_put_state(
                self.conf_room[conf_or_call_id].room_id,
                "org.matrix.msc3401.call.member",
                {"m.calls": []},
                state_key=self.client.user_id,
            )

        await conn.pc.close()

    async def leave_call(self, room: MatrixRoom) -> None:
        """
        Handler for the stop command.
        This sends the hangup events, cleans up the state we have, and resets the media.
        """

        if room.room_id in self.room_conf:
            conf_id = self.room_conf[room.room_id]

            for other in self.others[conf_id]:
                # Send Hangups
                unique_id = (other.user_id, conf_id)
                try:
                    conn = await asyncio.wait_for(self.__conns[unique_id], timeout=3)
                    await self.hangup(conf_id, conn)
                except asyncio.TimeoutError:
                    logger.warning("Gave up waiting for call on leave, task canceled")

            # End the connection
            await self.remove_connection(room)

            if room.room_id in self.__outputs:
                   del self.__outputs[room.room_id]

            # Notify user
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.notice",
                    "body": "Recording stopped",
                },
                ignore_unverified_devices=True,
            )
        else:
            # Notify user that we didnt have a running recording
            await self.client.room_send(
                room.room_id,
                "m.room.message",
                {
                    "msgtype": "m.notice",
                    "body": "No running recording was found.",
                },
                ignore_unverified_devices=True,
            )

    async def remove_connection(self, room: MatrixRoom) -> Optional[ConfID]:
        """
        This resets the connection for a call as well as removing the state.
        """
        if room.room_id in self.room_conf:
            call_id = self.room_conf[room.room_id]
            unique_id = (self.client.user_id, call_id)

            try:
                conn = await asyncio.wait_for(self.__conns[unique_id], timeout=3)
                await conn.pc.close()
                del self.__conns[unique_id]
            except asyncio.TimeoutError:
                logger.warning(
                    "Gave up waiting for call on remove connection, task canceled"
                )

            # del self.room_conf[room.room_id]
            # del self.conf_room[call_id]

            return call_id
        return None

    def track_others(
        self, conf_id: ConfID, device_id: str, user_id: UserID, session_id: str
    ) -> None:
        """
        Adds members of a call to the internal state.
        """

        if conf_id not in self.others:
            self.others[conf_id] = []
        self.others[conf_id].append(Others(user_id, device_id, session_id))

    def remove_other(self, conf_id: ConfID, user_id: UserID) -> None:
        """
        Removes people that are tracked.

        This should be called if a member leaves.
        """
        self.others[conf_id] = [o for o in self.others[conf_id] if o.user_id != user_id]

    async def join_call(self, room: MatrixRoom) -> None:
        """
        Joins a Call.

        This usually only is used when !start is pressed in a element-call room.

        It sets up a new invite and candidates.
        """

        if room.room_id not in self.room_conf:
            logger.warning(
                f"""
                Room {room.room_id} is not a call room or we forgot. 
                Trying to get call id from state.
                """
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

        # Borked without GLARE
        # await self.send_offer(room)

        conf_id = self.room_conf[room.room_id]
        await self.client.room_put_state(
            room.room_id,
            "org.matrix.msc3401.call.member",
            {
                "m.calls": [
                    {
                        "m.call_id": conf_id,
                        "m.devices": [
                            {
                                "device_id": self.client.device_id,
                                "expires_ts": int(time.time() * 1000)
                                + (1000 * 60 * 60),
                                "session_id": self.session_id,
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

    async def send_offer(self, room: MatrixRoom) -> None:
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
                #pc = RTCPeerConnection(STUN)
                pc = RTCPeerConnection()
                logger.info(f"Started ice for {call_id}")
                unique_id = (data.user_id, self.room_conf[room.room_id])
                conn = WrappedConn(
                    pc=pc,
                    candidate_waiter=None,
                    prepare_waiter=None,
                    room_id=room.room_id if room else None,
                    input_tracks={},
                )

                if room.room_id not in self.__outputs:
                    self.__outputs[room.room_id] = ProxyTrack(
                        MediaPlayer(
                            "./black.png",
                            options={"loop": "1", "framerate": "1", "hwaccel": "auto", "c:v": "h264", "preset:v": "ultrafast"},
                        )
                    )

                logger.info(f"Created connection {unique_id}")

                conn.pc.addTrack(self.__outputs[room.room_id])
                conn.pc.addTransceiver("audio", "sendrecv")

                logger.info("Adding tracks")

                base_path = os.path.join(RECORDING_PATH, f"{conf_id}")
                if not os.path.exists(base_path):
                    os.mkdir(base_path)
                base_name_audio = f"{data.user_id}_{call_id}"
                base_name_video = f"{data.user_id}_{call_id}"

                (wav_file, mp4_file) = self.get_filenames(
                    base_path, base_name_audio, base_name_video
                )

                logger.info("Setting up callbacks")

                pc.on(
                    "connectionstatechange",
                    lambda conn=conn, unique_id=unique_id, wav_file=wav_file, mp4_file=mp4_file, conf_id=conf_id: self.on_connectionstatechange(
                        conn, unique_id, wav_file, mp4_file, conf_id
                    ),
                )

                pc.on(
                    "track",
                    lambda track, conn=conn, user_id=data.user_id: self.on_track(
                        track, conn, user_id
                    ),
                )

                offer = await conn.pc.createOffer()
                await conn.pc.setLocalDescription(offer)

                logger.info(f"Got local candidates for {call_id}")
                candidates: List[Tuple[RTCIceCandidate, str]] = []
                for transceiver in conn.pc.getTransceivers():
                    gatherer: RTCIceGatherer = (
                        transceiver.sender.transport.transport.iceGatherer
                    )
                    for candidate in gatherer.getLocalCandidates():
                        candidate.sdpMid = transceiver.mid
                        candidates.append(
                            (
                                candidate,
                                str(gatherer.getLocalParameters().usernameFragment),
                            )
                        )

                # We set this late to make sure the connection is actually set up before we handle the answer.
                # Order of operation is weird otherwise.
                self.__conns[unique_id] = conn

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
                        "sender_session_id": self.session_id,
                        "dest_session_id": data.session_id,
                        "capabilities": {
                            "m.call.transferee": False,
                            "m.call.dtmf": False,
                        },
                        "org.matrix.msc3077.sdp_stream_metadata": {
                            pc._RTCPeerConnection__stream_id: {  # type: ignore
                                "purpose": "m.usermedia",
                                "audio_muted": True,
                                "video_muted": False,
                            }
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
                        "sender_session_id": self.session_id,
                        "dest_session_id": data.session_id,
                    },
                )
                await self.client.to_device(candidates_message)

    # async def handle_negotiation(
    #    self,
    #    room: Optional[MatrixRoom],
    #    event: [ToDeviceCallNegotiateEvent, CallNegotiateEvent],
    # ):
    #    pass

    def on_track(
        self,
        track: MediaStreamTrack,
        conn: WrappedConn,
        user_id: str,
        output_track: Optional[ProxyTrack] = None,
    ) -> None:
        if track.kind == "audio":
            logger.info(
                f"Adding audio track to recording for {user_id} when state {conn.pc.connectionState}"
            )
            conn.input_tracks["audio"] = track
        elif track.kind == "video":
            logger.info(
                f"Adding video track to recording for {user_id} when state {conn.pc.connectionState}"
            )
            conn.input_tracks["video"] = track
            if conn.pc and output_track:
                conn.pc.addTrack(output_track)

    async def on_connectionstatechange(
        self,
        conn: WrappedConn,
        unique_id: UniqueCallID,
        wav_file: str,
        mp4_file: str,
        conf_id: ConfID,
    ) -> None:
        if conn.pc.connectionState == "failed":
            logger.warn(f'State changed to "failed" for {unique_id}')
            await conn.pc.close()
            del self.__conns[unique_id]
        if conn.pc.connectionState == "connected":
            logger.info(f'State changed to "connected" for {unique_id}')
            asyncio.create_task(
                self.start_recording(wav_file, mp4_file, conn.input_tracks, conf_id)
            )

    async def handle_call_answer(self, event: ToDeviceCallAnswerEvent) -> None:
        """
        Handles the ToDevice call answer event.

        We only handle the to_device variant since we only initiate the call within 1:1 rooms.
        """
        logger.info(f"Received call answer from {event.sender}")

        unique_id: UniqueCallID = (event.sender, event.conf_id)
        logger.info(f"Received call answer for {unique_id}")

        if event.conf_id not in self.conf_room:
            logger.warning("Got invalid to-device call answer")
            return

        try:
            conn = await asyncio.wait_for(self.__conns[unique_id], timeout=3)
        except asyncio.TimeoutError:
            logger.warning("Gave up waiting for call on call answer, task canceled")
            return
        except KeyError:
            logger.warning("Received answer for unknown call")
            return

        logger.info(f"Setting remote description for {unique_id}")
        await conn.pc.setRemoteDescription(
            RTCSessionDescription(
                sdp=str(event.answer.get("sdp")), type=str(event.answer.get("type"))
            )
        )

        stats = await conn.pc.getStats()
        print(f"Stats for {unique_id}: {stats}")
        receivers = conn.pc.getTransceivers()
        for receiver in receivers:
            print(
                f"Transceiver kind for {unique_id}: {receiver.kind} - {receiver.currentDirection}"
            )

        logger.info("Adding tracks")

        if isinstance(event, CallInviteEvent):
            base_path = RECORDING_PATH
        else:
            base_path = os.path.join(RECORDING_PATH, f"{event.conf_id}")
        if not os.path.exists(base_path):
            os.mkdir(base_path)
        base_name_audio = f"{event.sender}_{event.call_id}"
        base_name_video = f"{event.sender}_{event.call_id}"

        (wav_file, mp4_file) = self.get_filenames(
            base_path, base_name_audio, base_name_video
        )

        logger.info("Setting up callbacks")

        conn.pc.on(
            "track",
            lambda track, conn=conn, user_id=event.sender: self.on_track(
                track, conn, user_id
            ),
        )

        conn.pc.on(
            "connectionstatechange",
            lambda conn=conn, unique_id=unique_id, wav_file=wav_file, mp4_file=mp4_file, conf_id=event.call_id if isinstance(
                event, CallInviteEvent
            ) else event.conf_id: self.on_connectionstatechange(
                conn, unique_id, wav_file, mp4_file, conf_id
            ),
        )

        others = self.others[event.conf_id]
        data = next((x for x in others if x.user_id == event.sender), None)

        if not data:
            logger.warning("Received answer for unknown call")
            return

        logger.info(f"Sending select_answer for {unique_id}")
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
                "sender_session_id": self.session_id,
                "dest_session_id": data.session_id,
            },
        )
        await self.client.to_device(message)

    def get_filenames(
        self, base_path: str, base_name_audio: str, base_name_video: str
    ) -> Tuple[str, str]:
        if os.path.exists(os.path.join(base_path, f"{base_name_audio}.wav")):
            i = 1
            while os.path.exists(os.path.join(base_path, f"{base_name_audio}_{i}.wav")):
                i += 1
            base_name_audio = f"{base_name_audio}_{i}"

        if os.path.exists(os.path.join(base_path, f"{base_name_video}.mp4")):
            i = 1
            while os.path.exists(os.path.join(base_path, f"{base_name_video}_{i}.mp4")):
                i += 1
            base_name_video = f"{base_name_video}_{i}"
        wav_file = os.path.join(base_path, f"{base_name_audio}")
        mp4_file = os.path.join(base_path, f"{base_name_video}")
        return (wav_file, mp4_file)

    async def start_recording(
        self, wav_file: str, mp4_file: str, input_tracks: InputTracks, conf_id: ConfID
    ) -> None:
        if "audio" in input_tracks:
            track_id = input_tracks["audio"].id
            wav_file_track_id = f"{wav_file}_{track_id}.wav"
            logger.info(f"Starting audio recorder for {wav_file_track_id}")
            audio_recorder = MediaRecorder(wav_file_track_id, format="wav")
            audio_recorder.addTrack(input_tracks["audio"])
            await audio_recorder.start()
            logger.info(f"Started audio recorder for {wav_file_track_id}")
        if "video" in input_tracks:
            track_id = input_tracks["video"].id
            mp4_file_track_id = f"{mp4_file}_{track_id}.mp4"
            logger.info(f"Starting video recorder for {mp4_file_track_id}")
            video_recorder = MediaRecorder(mp4_file_track_id, format="mp4")
            video_recorder.addTrack(input_tracks["video"])
            await video_recorder.start()
            logger.info(f"Started video recorder for {mp4_file_track_id}")
        else:
            video_recorder = None

        if "audio" in input_tracks:
            audio_track = input_tracks["audio"]

            @audio_track.on("ended")
            async def on_ended_audio():
                if audio_recorder:
                    logger.info(f"Audio ended for {wav_file}")
                    await audio_recorder.stop()

        if "video" in input_tracks:
            video_track = input_tracks["video"]

            @video_track.on("ended")
            async def on_ended_video():
                if video_recorder:
                    logger.info(f"Video ended for {mp4_file}")
                    await video_recorder.stop()

        if conf_id not in self.recording_rooms:
            self.recording_rooms.append(conf_id)
            await self.client.room_send(
                self.conf_room[conf_id].room_id,
                "m.room.message",
                {
                    "msgtype": "m.notice",
                    "body": "Successfully started recording",
                },
                ignore_unverified_devices=True,
            )

    async def handle_call_invite(
        self,
        event: Union[CallInviteEvent, ToDeviceCallInviteEvent],
        room: Optional[MatrixRoom],
    ) -> None:
        """
        Handle any invite we get. The main caller is 1:1 calls.
        """
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

        #pc = RTCPeerConnection(STUN)
        pc = RTCPeerConnection()
        room_id = ""
        if room:
            room_id = room.room_id
        else:
            if isinstance(event, ToDeviceCallInviteEvent):
                room_id = self.conf_room[event.conf_id].room_id

        if room_id not in self.__outputs:
            self.__outputs[room_id] = ProxyTrack(
                MediaPlayer(
                    "./black.png",
                    options={"loop": "1", "framerate": "1", "hwaccel": "auto"},
                )
            )
        pc.addTrack(self.__outputs[room_id])
        if isinstance(event, CallInviteEvent):
            unique_id: UniqueCallID = (event.sender, event.call_id)
        else:
            unique_id = (event.sender, event.conf_id)
        conn = self.__conns[unique_id] = WrappedConn(
            pc=pc,
            candidate_waiter=self.loop.create_future(),
            prepare_waiter=self.loop.create_future(),
            input_tracks={},
            room_id=room.room_id if room else None,
        )

        logger.info("Adding tracks")

        if isinstance(event, CallInviteEvent):
            base_path = RECORDING_PATH
        else:
            base_path = os.path.join(RECORDING_PATH, f"{event.conf_id}")
        if not os.path.exists(base_path):
            os.mkdir(base_path)
        base_name_audio = f"{event.sender}_{event.call_id}"
        base_name_video = f"{event.sender}_{event.call_id}"

        (wav_file, mp4_file) = self.get_filenames(
            base_path, base_name_audio, base_name_video
        )

        logger.info("Setting up callbacks")

        conn.pc.on(
            "track",
            lambda track, conn=conn, user_id=event.sender: self.on_track(
                track, conn, user_id
            ),
        )

        conn.pc.on(
            "connectionstatechange",
            lambda conn=conn, unique_id=unique_id, wav_file=wav_file, mp4_file=mp4_file, conf_id=event.call_id if isinstance(
                event, CallInviteEvent
            ) else event.conf_id: self.on_connectionstatechange(
                conn, unique_id, wav_file, mp4_file, conf_id
            ),
        )

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
            del self.__conns[unique_id]
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
                "org.matrix.msc3077.sdp_stream_metadata": {
                    pc._RTCPeerConnection__stream_id: {  # type: ignore
                        "purpose": "m.usermedia",
                        "audio_muted": True,
                        "video_muted": False,
                    }
                },
                "answer": {
                    "type": pc.localDescription.type,
                    "sdp": pc.localDescription.sdp,
                },
                "device_id": self.client.device_id,
                "dest_session_id": event.source["content"]["sender_session_id"],
                "sender_session_id": self.session_id,
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
        elif isinstance(event, ToDeviceCallInviteEvent):
            logger.info(
                f"Sent answer to {event.sender} with device {event.source['sender_device']}"
            )

    async def handle_call_candidates(
        self,
        room: Optional[MatrixRoom],
        event: Union[CallCandidatesEvent, ToDeviceCallCandidatesEvent],
    ) -> None:
        """
        Handle call candidates we get and add them to the connection.
        """

        if room:
            logger.info(
                f"Received call candidates from {event.sender} in {room.room_id}"
            )
        else:
            logger.info(f"Received call candidates from {event.sender}")

        if isinstance(event, CallCandidatesEvent):
            unique_id: UniqueCallID = (event.sender, event.call_id)
        else:
            unique_id = (event.sender, event.conf_id)

            if event.conf_id not in self.conf_room:
                logger.warning("Got invalid to-device call candidates")
                return

        try:
            conn = await asyncio.wait_for(self.__conns[unique_id], timeout=10)
        except asyncio.TimeoutError:
            logger.warning("Gave up waiting for call candidates, task canceled")
            return
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
        """
        Handles the hangup.

        Currently this only closes the connection.
        """

        if event.sender == self.client.user_id:
            return
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

        # TODO: This is incorrect:
        # The session ele-web sends on new_session is dead
        if reason == "replaced" or reason == "new_session":
            logger.warning("Call was replaced but we ignore that for now")
            return

        try:
            if isinstance(event, CallHangupEvent):
                unique_id: UniqueCallID = (event.sender, event.call_id)
            else:
                unique_id = (event.sender, event.conf_id)
            try:
                conn = await asyncio.wait_for(self.__conns[unique_id], timeout=3)
                await conn.pc.close()
            except asyncio.TimeoutError:
                logger.warning("Gave up waiting for call on hangup, task canceled")
            if room and isinstance(event, CallHangupEvent):
                await self.client.update_receipt_marker(room.room_id, event.event_id)

        except KeyError:
            logger.warning("Received hangup for unknown call")
            return
