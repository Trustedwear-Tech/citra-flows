"""Entry-point shim — `python main.py` matches the local-dev launch
convention used across other Citra services. Delegates to worker._main().
The canonical module entry point `python -m worker` still works."""
import asyncio

import worker

if __name__ == "__main__":
    try:
        asyncio.run(worker._main())
    except KeyboardInterrupt:
        pass
