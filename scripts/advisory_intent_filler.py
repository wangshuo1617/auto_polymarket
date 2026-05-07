"""CLI: run advisory intent_filler once."""
import json, logging, os, sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.advisory.intent_filler import run_once  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
res = run_once()
print(json.dumps(res.as_dict(), default=str))
sys.exit(0 if res.errors == 0 else 1)
