"""Shared Mini FTP framing helpers.

TCP is a byte stream. A single send() may arrive as several recv() calls, and
several sends may arrive in one recv(). Commands are therefore one UTF-8 line
ending in "\\n". File bytes are not lines: the sender first announces the exact
size, and the receiver reads that many bytes.

Both server.py and client.py import this module so they follow the same rules.

Protocol
--------
Client request lines:
    LIST
    UPLOAD <filename> <size>
    DOWNLOAD <filename>
    QUIT

UPLOAD:
    C: UPLOAD <filename> <size>\\n
    S: OK Ready\\n          or    ERR <reason>\\n
    C: exactly <size> raw bytes, only after "OK Ready"
    S: OK Uploaded <filename> (<size> bytes)\\n
       OK Replaced <filename> (<size> bytes)\\n
       ERR <reason>\\n

DOWNLOAD:
    C: DOWNLOAD <filename>\\n
    S: OK <size>\\n
    S: exactly <size> raw bytes
    or:
    S: ERR <reason>\\n

LIST:
    C: LIST\\n
    S: OK <count>\\n
    then <count> lines of: <filename> <size>

QUIT:
    C: QUIT\\n
    S: OK Goodbye\\n

Filenames use only letters, numbers, dots, underscores, and hyphens. They cannot
contain a folder path. That keeps LIST lines unambiguous and blocks traversal.
"""

import os
import stat
import threading
import time


CHUNK_SIZE = 64 * 1024
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_LINE_LENGTH = 4096
CONNECT_TIMEOUT = 10
SOCKET_TIMEOUT = 300

_FILENAME_MAX = 255


class ProtocolError(Exception):
    """The bytes on the socket do not follow the Mini FTP protocol."""


class TransferError(Exception):
    """A request was refused, or it cannot be completed safely."""


def safe_filename(filename):
    """Return filename if it is a single safe name, otherwise raise ValueError.

    Rejects empty names, folder separators, and ".." so a client cannot ask the
    server to open files outside the storage folder.
    """
    if not isinstance(filename, str):
        raise ValueError("Invalid filename.")
    name = filename.strip()
    if not name:
        raise ValueError("File name cannot be empty.")
    if len(name) > _FILENAME_MAX:
        raise ValueError("Filename is too long.")
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(
            "Invalid filename. Use a file name only, without a folder path."
        )
    # A leading dot is reserved for temporary transfer files.
    first = name[0]
    if not (("A" <= first <= "Z") or ("a" <= first <= "z") or ("0" <= first <= "9")):
        raise ValueError(
            "Invalid filename. Start with a letter or number, and use only "
            "letters, numbers, dots, underscores, and hyphens."
        )
    for char in name:
        if not (
            ("A" <= char <= "Z")
            or ("a" <= char <= "z")
            or ("0" <= char <= "9")
            or char in "._-"
        ):
            raise ValueError(
                "Invalid filename. Use only letters, numbers, dots, "
                "underscores, and hyphens."
            )
    return name


def safe_join(directory, filename):
    """Join directory and filename, and require the result to stay inside directory."""
    name = safe_filename(filename)
    root = os.path.realpath(directory)
    path = os.path.join(root, name)
    parent = os.path.realpath(os.path.dirname(path))
    if parent != root:
        raise ValueError("Invalid filename. Use a file name only, without a folder path.")
    return path


def is_regular_file(path):
    """True only for a real file. Symlinks return False and are not followed."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def cleanup_partial_files(directory):
    """Delete leftover temp files from transfers that were interrupted."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith(".partial-"):
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
        except OSError:
            pass


def create_partial_file(directory):
    """Create a new hidden temp file inside directory and return (path, fd)."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    last_error = None
    for _ in range(5):
        name = f".partial-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        path = os.path.join(directory, name)
        try:
            fd = os.open(path, flags, 0o644)
            return path, fd
        except FileExistsError as exc:
            last_error = exc
    raise OSError("Could not create a temporary file for the transfer.") from last_error


def send_line(sock, text):
    """Send one protocol line, including the newline."""
    if not isinstance(text, str):
        raise ProtocolError("Protocol message must be text.")
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ProtocolError("Protocol message cannot contain a newline.")
    sock.sendall((text + "\n").encode("utf-8"))


def recv_exact(sock, size):
    """Read exactly size bytes, even if the OS returns them in small pieces."""
    if size < 0:
        raise ProtocolError("Invalid byte count.")
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Connection closed before all bytes were received.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_line(sock, max_len=MAX_LINE_LENGTH):
    """Read one UTF-8 line, or return None if the peer closed the connection.

    One byte is read at a time on purpose. Command lines are short, and this
    avoids holding extra bytes that actually belong to the file that follows.
    """
    buf = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            if buf:
                raise ConnectionError("Connection closed in the middle of a command.")
            return None
        byte = chunk[0]
        if byte == 10:  # newline
            break
        if byte == 13:  # carriage return from a \\r\\n line ending
            continue
        if len(buf) >= max_len:
            raise ProtocolError("Command is too long.")
        buf.append(byte)
    try:
        return buf.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("Command is not valid UTF-8.") from exc


def read_status(sock):
    """Read an OK or ERR line. Returns (ok, message)."""
    line = recv_line(sock)
    if line is None:
        raise ConnectionError("Server closed the connection.")
    if line == "OK":
        return True, ""
    if line.startswith("OK "):
        return True, line[3:]
    if line == "ERR":
        return False, "Unknown server error."
    if line.startswith("ERR "):
        return False, line[4:]
    raise ProtocolError(f"Unexpected server response: {line}")


def send_file(sock, file_obj, size):
    """Send exactly size bytes from file_obj in chunks."""
    if size < 0 or size > MAX_FILE_SIZE:
        raise ProtocolError("Invalid file size.")
    remaining = size
    while remaining:
        to_read = CHUNK_SIZE if remaining > CHUNK_SIZE else remaining
        chunk = file_obj.read(to_read)
        if len(chunk) != to_read:
            raise TransferError("The file ended before the expected number of bytes were sent.")
        sock.sendall(chunk)
        remaining -= len(chunk)


def recv_file(sock, file_obj, size):
    """Receive exactly size bytes and write them to file_obj in chunks."""
    if size < 0 or size > MAX_FILE_SIZE:
        raise ProtocolError("Invalid file size.")
    remaining = size
    while remaining:
        block_size = CHUNK_SIZE if remaining > CHUNK_SIZE else remaining
        file_obj.write(recv_exact(sock, block_size))
        remaining -= block_size
