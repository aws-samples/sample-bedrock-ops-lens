"""Explicit outcomes shared by ingesters and their Lambda runner."""
from enum import IntEnum


class IngestOutcome(IntEnum):
    # Retain CLI exit code 2, but use identity (not equality) in the runner:
    # a plain return 2 or argparse's SystemExit(2) is still an error.
    TIME_BUDGET_EXHAUSTED = 2
