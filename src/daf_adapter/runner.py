"""Execute one explicitly approved command with bounded capture and deadline."""

import asyncio

from .protocol import validate_request


async def run(value):
    """Run argv without a shell. Cancellation kills and reaps the direct child.

    This is not a sandbox. Environment and filesystem access are inherited.
    Descendants are not killed; inherited pipes are bounded by the deadline.
    """
    request = validate_request(value)
    result = {
        "schema_version": 1, "id": request["id"], "status": "failed", "exit_code": None,
        "stdout": "", "stderr": "", "stdout_truncated": False, "stderr_truncated": False,
    }
    capture = {"stdout": bytearray(), "stderr": bytearray()}
    limit = request["max_output_bytes"]
    process = None
    tasks = []

    async def drain(stream, key):
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            remaining = limit - len(capture[key])
            capture[key].extend(chunk[:remaining])
            if len(chunk) > remaining:
                result[key + "_truncated"] = True

    async def feed():
        try:
            payload = (request.get("stdin") or "").encode("utf-8")
            for offset in range(0, len(payload), 8192):
                process.stdin.write(payload[offset:offset + 8192])
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def execute():
        nonlocal process, tasks
        process = await asyncio.create_subprocess_exec(
            *request["argv"], cwd=request.get("cwd"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8192,
        )
        tasks = [asyncio.create_task(drain(process.stdout, "stdout")),
                 asyncio.create_task(drain(process.stderr, "stderr")), asyncio.create_task(feed())]
        await asyncio.gather(process.wait(), *tasks)
        result["exit_code"] = process.returncode
        result["status"] = "succeeded" if process.returncode == 0 else "failed"

    async def cleanup():
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Pipe EOF can be held by an independently surviving descendant.
            # Close pipe transports after cancelling readers, then reap our child.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (process.stdout, process.stderr):
                transport = getattr(stream, "_transport", None)
                if transport is not None:
                    transport.close()
            if process.stdin is not None:
                process.stdin.close()
            await process.wait()

    try:
        await asyncio.wait_for(execute(), request["timeout_ms"] / 1000)
    except asyncio.TimeoutError:
        result["status"] = "timed_out"
        await cleanup()
    except asyncio.CancelledError:
        await cleanup()
        raise
    except (OSError, ValueError) as error:
        message = str(error).encode("utf-8")
        remaining = limit - len(capture["stderr"])
        capture["stderr"].extend(message[:remaining])
        result["stderr_truncated"] |= len(message) > remaining
        await cleanup()
    finally:
        for key in capture:
            result[key] = bytes(capture[key]).decode("utf-8", errors="replace")
    return result
