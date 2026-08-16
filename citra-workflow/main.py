"""Entry-point shim — `python main.py` matches the local-dev launch
convention used across other Citra services. Boots the workflow service
on the configured port (default 9200)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "citra_workflow.main:app",
        host=os.getenv("WORKFLOW_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("WORKFLOW_SERVICE_PORT", "9200")),
        reload=os.getenv("WORKFLOW_SERVICE_RELOAD", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
