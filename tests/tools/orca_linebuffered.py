#!/usr/bin/env python3
"""Runs Orca with its debug file line-buffered, for tests that read what
Orca says as it says it (Orca opens the file block-buffered, so its last
utterances sit in memory until more is logged).

Usage: orca_linebuffered.py DEBUG_FILE [orca args...]
"""
import builtins
import runpy
import shutil
import sys

log = sys.argv[1]
_open = builtins.open


def line_buffered_open(file, mode="r", *args, **kwargs):
    if file == log and "w" in mode:
        kwargs["buffering"] = 1
        return _open(file, mode, **kwargs)
    return _open(file, mode, *args, **kwargs)


builtins.open = line_buffered_open
sys.argv = ["orca", *sys.argv[2:], "--debug-file", log]
runpy.run_path(shutil.which("orca"), run_name="__main__")
