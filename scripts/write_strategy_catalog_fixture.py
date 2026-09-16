"""Write the agent's published strategy catalog as a frontend test fixture.

The frontend tests mock the API; using the agent's *real* document (rather than a
hand-written stub) means they render from the same contract the backend serves.

Regenerate from the repository root with:

    .venv/bin/python scripts/write_strategy_catalog_fixture.py

(``tmp/`` is gitignored, so the script lives in ``scripts/``.)

Two properties keep the tree clean, and both are deliberate:

* ``published_at`` is a wall clock, so it is carried over from the existing file
  whenever nothing else in the document changed. The value then means "when this
  snapshot last changed" instead of "when the generator last ran", and a run that
  changes nothing leaves no diff to explain away.
* The result is **not** run through Biome: ``frontend/biome.json`` excludes this
  file. Biome collapses short arrays onto one line while the generator expands
  them, so leaving the file to Biome made every ``bun run lint`` rewrite it and
  hand the author an unrelated change to revert (TASKS.md T03). The committed
  formatting is the generator's, and the file is a generated artifact.
"""

import json
from pathlib import Path
from typing import Any

from agent.protocol import describe_catalog

#: Relative to the repository root.
target = Path("frontend/tests/fixtures/strategy-catalog.json")


def without_timestamp(document: dict[str, Any]) -> dict[str, Any]:
    """The document minus ``published_at``, so two snapshots can be compared."""
    return {key: value for key, value in document.items() if key != "published_at"}


def main() -> None:
    document = describe_catalog()

    if target.exists():
        previous = json.loads(target.read_text())
        if previous.get("published_at") and without_timestamp(
            previous
        ) == without_timestamp(document):
            document["published_at"] = previous["published_at"]

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
