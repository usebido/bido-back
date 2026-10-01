"""`python -m bido_back.worker` runs the SQS worker until SIGTERM."""

import asyncio

from bido_back.worker.main import main

if __name__ == "__main__":
    asyncio.run(main())
