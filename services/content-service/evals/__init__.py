"""Session fixtures: real recordings kept as regression tests for the summariser.

The point of this package is to make "did that change make summaries better or
worse?" a question with an answer. Everything here is a DEV TOOL: nothing under
app/ imports it, and it can be deleted in one move (see README.md).

Two layers, deliberately separate:

* `fixtures` - the data. A fixture is a DIRECTORY under evals/fixtures/ holding
  a frozen transcript, the draft that shipped from it, and the checks it must
  satisfy. Discovered by scanning, so removing one is deleting its directory.
* `checks` - the engine. Pure and LLM-free (the judge is injected), so it stays
  fully unit-tested after every fixture has been removed.

`runner` drives the real content-service pipeline over a fixture and writes both
the report and every intermediate artefact, so two runs can be diffed instead of
argued about.
"""

from evals import checks, fixtures

__all__ = ["checks", "fixtures"]
