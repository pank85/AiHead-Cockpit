"""Private stdio entrypoint. No terminal operations and no Textual runtime."""
from __future__ import annotations

import asyncio
import sys

from .protocol import MAX_MESSAGE_BYTES


async def _main(stdin, stdout) -> None:
    # Import integrations only after main() has reserved stdout for the wire.
    from .server import serve

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    read_transport, _ = await loop.connect_read_pipe(lambda: reader_protocol, stdin)
    write_transport, write_protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, stdout)
    writer = asyncio.StreamWriter(write_transport, write_protocol, reader, loop)
    try:
        await serve(reader, writer)
    finally:
        read_transport.close()
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionError, OSError, NotImplementedError):
            pass


def main() -> None:
    # The original binary stdout belongs to the wire writer alone. Even incidental
    # prints from imported integrations go to stderr rather than corrupting JSON.
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr
    try:
        asyncio.run(_main(stdin, stdout))
    except (BrokenPipeError, ConnectionError):
        pass


if __name__ == "__main__":
    main()
