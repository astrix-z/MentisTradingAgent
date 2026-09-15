"""Write the agent's published strategy catalog as a frontend test fixture.

The frontend tests mock the API; using the agent's *real* document (rather than a
hand-written stub) means they render from the same contract the backend serves.

Regenerate from the repository root with:

    .venv/bin/python scripts/write_strategy_catalog_fixture.py

(``tmp/`` is gitignored, so the script lives in ``scripts/``. Only
``published_at`` differs between runs; ``schema_hash`` covers the strategies
and is stable.)
"""

import json
from pathlib import Path

from agent.protocol import describe_catalog

#: Relative to the repository root.
target = Path("frontend/tests/fixtures/strategy-catalog.json")
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(describe_catalog(), indent=2) + "\n")
print(f"wrote {target} ({target.stat().st_size} bytes)")
