"""Run the pinned Open Terminal API with a wall-time limit for tool commands."""

import asyncio
import os

import uvicorn
from open_terminal import config


async def limit_runtime(process, seconds):
    try:
        await asyncio.wait_for(process.wait(), timeout=seconds)
    except TimeoutError:
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except TimeoutError:
            process.kill(force=True)


def build_app(seconds):
    config.init(isolated=True)
    from open_terminal import main

    start = main.create_runner
    timers = set()

    async def start_timed_command(*args, **kwargs):
        process = await start(*args, **kwargs)
        task = asyncio.create_task(limit_runtime(process, seconds))
        timers.add(task)
        task.add_done_callback(timers.discard)
        return process

    main.create_runner = start_timed_command
    return main.app


if __name__ == "__main__":
    seconds = int(os.environ["OPEN_TERMINAL_COMMAND_SECONDS"])
    uvicorn.run(build_app(seconds), host="0.0.0.0", port=8000, access_log=False)
