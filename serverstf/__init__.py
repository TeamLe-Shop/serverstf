"""The :mod:`serverstf` package.

This package implements all the services used in running a SVTF instance.
"""

import argparse
import collections
import enum
import functools
import urllib.parse

import venusian


class FatalError(Exception):
    """Raised for unrecoverable errors."""


class ExitStatus(enum.IntEnum):
    """Possible exit statuses for :func:`serverstf.main`."""

    OK = 0  # pylint: disable=invalid-name
    FATAL_ERROR = 1
    UNEXPECTED_ERROR = 2
