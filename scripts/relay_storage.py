"""Verified local storage for the relay and its hook wrapper."""
import os
import stat
import sys
import time
import traceback
from contextlib import ExitStack, contextmanager


def data_root():
    return (os.environ.get("RELAY_ROOT") or os.environ.get("CLAUDE_PLUGIN_DATA")
            or os.path.join(os.path.expanduser("~"), ".session-relay"))


def trusted_directory(fd, name, *, private=False):
    info = os.fstat(fd)
    uid = os.geteuid()
    if private:
        trusted = info.st_uid == uid and not info.st_mode & 0o022
    else:
        # Sticky system-owned ancestors such as /tmp cannot be used by another
        # owner to replace one of our directory entries.
        trusted = info.st_uid in (0, uid) and (not info.st_mode & 0o022 or info.st_mode & stat.S_ISVTX)
    if not trusted:
        raise PermissionError("unsafe relay directory: " + name)


@contextmanager
def child_directory(parent, name, *, create=False, private=False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(name, flags, dir_fd=parent)
    try:
        trusted_directory(fd, name, private=private)
        yield fd
    finally:
        os.close(fd)


def absolute_path(path):
    path = os.path.abspath(path)
    if sys.platform == "darwin":
        # Darwin exposes these root-owned system aliases for actual directories.
        for alias in ("/tmp", "/var"):
            if path == alias or path.startswith(alias + "/"):
                info = os.lstat(alias)
                if (info.st_uid == 0 and stat.S_ISLNK(info.st_mode)
                        and os.readlink(alias) == "private" + alias):
                    return "/private" + path
    return path


@contextmanager
def directory(path, *, create=False, private=False):
    with ExitStack() as stack:
        fd = stack.enter_context(child_directory(None, "/"))
        parts = [part for part in absolute_path(path).split("/") if part]
        for index, part in enumerate(parts):
            fd = stack.enter_context(child_directory(fd, part, create=create,
                                                      private=private and index == len(parts) - 1))
        if private and not parts:
            trusted_directory(fd, "/", private=True)
        yield fd


def trusted_file(info, name, *, reading_snapshot=False):
    links = (0, 1) if reading_snapshot else (1,)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink not in links or info.st_mode & 0o022):
        raise PermissionError("unsafe relay file: " + name)
    return info


def same_file(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def open_file(parent, name, flags):
    """Open only an owned, regular file without following links or waiting on devices."""
    try:
        before = trusted_file(os.stat(name, dir_fd=parent, follow_symlinks=False), name)
    except FileNotFoundError:
        before = None
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    try:
        after = trusted_file(os.fstat(fd), name, reading_snapshot=flags == os.O_RDONLY)
        if before is not None and not same_file(before, after):
            # Legitimate atomic replacements can race readers. The newly opened
            # regular file is safe for read-only consumers of complete snapshots.
            if flags != os.O_RDONLY:
                raise PermissionError("relay file changed while opening: " + name)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def open_transcript(path):
    parent, name = os.path.split(absolute_path(path))
    with directory(parent) as fd:
        with os.fdopen(open_file(fd, name, os.O_RDONLY), "rb") as transcript:
            yield transcript


@contextmanager
def replacement(parent, name, mode):
    """Complete a same-directory file before atomically publishing it."""
    temp = ".relay-" + os.urandom(16).hex()
    fd = open_file(parent, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        options = {} if "b" in mode else {"encoding": "utf-8"}
        with os.fdopen(fd, mode, **options) as file:
            yield file
        try:
            trusted_file(os.stat(name, dir_fd=parent, follow_symlinks=False), name)
        except FileNotFoundError:
            pass
        os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temp, dir_fd=parent)
        except FileNotFoundError:
            pass


def log_error(root, where):
    report = traceback.format_exc()
    try:
        with directory(root, create=True, private=True) as parent:
            fd = open_file(parent, "errors.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            with os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace") as log:
                os.fchmod(log.fileno(), 0o600)
                log.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), where, report))
    except OSError:
        pass
