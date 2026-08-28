"""Shared logging configuration for the pipeline, agents, and utils modules."""

import logging


def configure_logging():
    """Set up a consistent log format/level across all entry points."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
