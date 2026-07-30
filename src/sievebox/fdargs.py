"""Write a NUL-separated token list to an anonymous memfd for bwrap --args FD.

The caller is responsible for setting the fd inheritable when it will be passed
to os.execvp (bwrap inherits the fd and reads it for setup directives). For
subprocess.Popen-based exec (e.g. strace), use pass_fds=(fd,) instead.
"""
from __future__ import annotations

import os


def write_args_fd(tokens: list[str]) -> int:
    """Write `tokens` NUL-separated into a new memfd, set it inheritable.

    Returns the raw fd number. The caller must close it after exec or
    subprocess completion.
    """
    fd = os.memfd_create("sievebox-args")
    data = "\0".join(tokens)
    os.write(fd, data.encode())
    os.set_inheritable(fd, True)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd
