"""
Handler modules. Importing this package side-imports every module so
the @register decorators run and populate the registry.

Add a new handler:
  1. Drop a file in this directory: e.g. `report_handlers.py`
  2. Inside, decorate async functions with @register("name").
  3. Import the module here: `from . import report_handlers`
"""
from . import workflow_handlers  # noqa: F401  — side-import for registration
