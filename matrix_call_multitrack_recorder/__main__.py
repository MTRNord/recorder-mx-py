# SPDX-FileCopyrightText: 2023-present MTRNord <support@nordgedanken.dev>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio

from logbook import Logger, StreamHandler
import logbook
import sys

from matrix_call_multitrack_recorder.bot import RecordingBot, login

StreamHandler(sys.stdout).push_application()
logger = Logger(__name__)
logger.level = logbook.INFO

# Entry

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
