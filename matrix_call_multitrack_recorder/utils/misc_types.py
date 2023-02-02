from typing import TypedDict

from aiortc import MediaStreamTrack


class InputTracks(TypedDict, total=False):
    audio: MediaStreamTrack
    video: MediaStreamTrack
