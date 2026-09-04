"""Restate endpoint: serves the two workflows and the pane object.

Run with:  uv run python -m reviewdemo.app
Then:      ./bin/restate deployments register http://localhost:9080 --yes
"""

from __future__ import annotations

import asyncio
import logging
import os

import restate

from .objects.pane import pane
from .workflows.agent import agent
from .workflows.review import review

logging.basicConfig(
    level=os.environ.get("RD_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

app = restate.app([review, agent, pane])


def main() -> None:
    import hypercorn
    import hypercorn.asyncio

    config = hypercorn.Config()
    config.bind = [f"0.0.0.0:{os.environ.get('RD_PORT', '9080')}"]
    # Restate speaks HTTP/2 to the SDK; keep long-lived streams alive.
    config.h2_max_concurrent_streams = 2147483647
    config.keep_alive_max_requests = 2147483647
    config.keep_alive_timeout = 2147483647
    asyncio.run(hypercorn.asyncio.serve(app, config))


if __name__ == "__main__":
    main()
