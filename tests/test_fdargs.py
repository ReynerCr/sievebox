"""Tests for the memfd-based bwrap arg writer (fdargs)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from sievebox.fdargs import write_args_fd


def test_write_args_fd_roundtrip():
    tokens = ["--tmpfs", "/", "--bind", "/home", "/home", "--setenv", "FOO", "bar"]
    fd = write_args_fd(tokens)
    assert os.get_inheritable(fd)
    data = os.read(fd, 4096)
    assert data == b"\0".join(t.encode() for t in tokens)
    os.close(fd)


def test_write_args_fd_empty():
    fd = write_args_fd([])
    data = os.read(fd, 4096)
    assert data == b""
    os.close(fd)


def test_write_args_fd_single_token():
    fd = write_args_fd(["--help"])
    data = os.read(fd, 4096)
    assert data == b"--help"
    assert os.get_inheritable(fd)
    os.close(fd)
