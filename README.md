# matrix-call-multitrack-recorder

[![PyPI - Version](https://img.shields.io/pypi/v/matrix-call-multitrack-recorder.svg)](https://pypi.org/project/matrix-call-multitrack-recorder)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/matrix-call-multitrack-recorder.svg)](https://pypi.org/project/matrix-call-multitrack-recorder)

-----

**Table of Contents**

- [Installation](#installation)
- [License](#license)

## Production-readyness

This bot currently is missing some polishing especially on stability and usability.
Also it doesnt work with rejoining currently or changing streams.
Therefor I do recommend it should not be used in production just yet.

## Installation

Due to changes to the nio dependency I currently cant upload it to pypi.
For now you have to install it from git directly:

```console
pip install git+https://git.nordgedanken.dev/mtrnord/matrix-call-multitrack-recorder.git
```

## Usage

Please Note that this is still in alpha.
State inconsistencies are to be expected.

### Starting/Stopping a recording

#### 1-1 Rooms

In 1-1 Rooms the recording starts automatically after ~1-2s.
Currently it will keep saying it is connecting.
There is a fix for this in the pipeline but yet to be written.

A call stops when the call ends.

#### Matrix-Call Rooms

Here the call doesn't automatically start.
Instead you need to run `!start`.
This can happen before or after joining.
The bot will write a message when the recording was successfully started.
In case the time isn't shown by the bot you should be able to make it update by changing rooms.

To stop the recording you run `!stop`.

The bot should record everyone joining late as well.
There may be a slight delay due to call handshaking before it actually starts.
New joining people may not see the Video currently. A fix is planned.

### Where are the recordings?

Currently they get stored to the working directory of the bot in a folder named `recordings`.
In that folder you find a folder per call or conference id.
In there you find each stream separate.
Note that changing the audio device, webcam being turned off and on again or screensharing
may cause new recording files as the webrtc connection might be rengotiated.
There is also a chance that this fails currently. A fix is on the way.

Later it is planned to be able to download them as well as optionally postprocess and merge them.

Please note that if you have a Element-Call Room in Element clients the call doesn't end when everyone leaves.
The bot currently therefor does not create a new folder.

## License

`matrix-call-multitrack-recorder` is distributed under the terms of the [AGPL-3.0-or-later](https://spdx.org/licenses/AGPL-3.0-or-later.html) license.
