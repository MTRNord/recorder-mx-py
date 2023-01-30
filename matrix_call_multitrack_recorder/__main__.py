# SPDX-FileCopyrightText: 2023-present MTRNord <support@nordgedanken.dev>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import uvloop
import sys

from logbook import Logger, StreamHandler
import logbook

from matrix_call_multitrack_recorder.bot import RecordingBot, login

StreamHandler(sys.stdout).push_application()
logger = Logger(__name__)
logger.level = logbook.INFO

# Entry

BOT = None


async def main() -> None:
    logger.info("Starting bot...")
    client = await login()
    global BOT
    BOT = RecordingBot(client=client)

    await BOT.start()


if __name__ == "__main__":

    if sys.version_info >= (3, 11):
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            try:
                runner.get_loop().run_until_complete(main())
            except Exception:
                if BOT:
                    runner.get_loop().run_until_complete(BOT.stop())
                logger.exception("Fatal Runtime error.")
                sys.exit(1)
            except KeyboardInterrupt:
                if BOT:
                    runner.get_loop().run_until_complete(BOT.stop())
                print("Received keyboard interrupt.")
                sys.exit(0)

    else:
        uvloop.install()
        try:
            asyncio.get_event_loop().run_until_complete(main())
        except Exception:
            if BOT:
                asyncio.get_event_loop().run_until_complete(BOT.stop())
            logger.exception("Fatal Runtime error.")
            sys.exit(1)
        except KeyboardInterrupt:
            if BOT:
                asyncio.get_event_loop().run_until_complete(BOT.stop())
            print("Received keyboard interrupt.")
            sys.exit(0)
