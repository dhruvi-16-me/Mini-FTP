"""Mini FTP client.

Connects to the server with TCP and provides a small text menu:
upload a file, download a file, list server files, or exit.

Local files are read from and written to client_files/.

Run:
    python3 client.py
    python3 client.py 127.0.0.1 5001
    python3 client.py 127.0.0.1 5001 client_files
"""

import os
import socket
import stat
import sys

from protocol import (
    CONNECT_TIMEOUT,
    MAX_FILE_SIZE,
    SOCKET_TIMEOUT,
    ProtocolError,
    TransferError,
    cleanup_partial_files,
    create_partial_file,
    is_regular_file,
    read_status,
    recv_file,
    recv_line,
    safe_filename,
    safe_join,
    send_file,
    send_line,
)

HOST = "127.0.0.1"
PORT = 5001
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.join(BASE_DIR, "client_files")


def say(message=""):
    print(message, flush=True)


def parse_port(value):
    text = str(value)
    if not text.isdigit():
        raise ValueError(f"invalid port: {value}")
    port = int(text)
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def connect_to_server(host, port):
    """Open a TCP connection to the Mini FTP server."""
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    sock.settimeout(SOCKET_TIMEOUT)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    return sock


def disconnect(sock):
    """Tell the server we are done, then close the socket."""
    try:
        send_line(sock, "QUIT")
        read_status(sock)
    except (OSError, ProtocolError, TransferError):
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass


def local_files(folder):
    """Return sorted (name, size) pairs that this client is allowed to upload."""
    entries = []
    for name in os.listdir(folder):
        try:
            safe_filename(name)
        except ValueError:
            continue
        path = os.path.join(folder, name)
        if not is_regular_file(path):
            continue
        try:
            size = os.lstat(path).st_size
        except OSError:
            continue
        entries.append((name, size))
    entries.sort(key=lambda item: item[0].lower())
    return entries


def upload_file(sock, folder, filename):
    """Upload one local file. Returns the server success message.

    Checks the local file before sending a command. The file bytes are sent
    only after the server replies "OK Ready", so an error does not mix the
    command stream with file data.
    """
    name = safe_filename(filename)
    path = safe_join(folder, name)
    if not os.path.lexists(path):
        raise FileNotFoundError(f"Local file not found: {name}")
    if not is_regular_file(path):
        raise ValueError(f"Not a regular file: {name}")
    size = os.lstat(path).st_size
    if size > MAX_FILE_SIZE:
        raise ValueError(f"File is too large. The maximum is {MAX_FILE_SIZE} bytes.")

    send_line(sock, f"UPLOAD {name} {size}")
    ok, message = read_status(sock)
    if not ok:
        raise TransferError(message)
    if message != "Ready":
        raise ProtocolError("Server did not accept the upload.")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        # The server is now waiting for size bytes. Closing is safer than
        # leaving the two sides out of step.
        _shutdown(sock)
        raise ConnectionError(f"Could not read {name}: {exc.strerror or exc}") from exc
    try:
        with os.fdopen(fd, "rb") as infile:
            info = os.fstat(infile.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise TransferError("The local file changed before it could be sent.")
            send_file(sock, infile, size)
        ok, message = read_status(sock)
    except TransferError:
        _shutdown(sock)
        raise ConnectionError("Upload failed and the connection was closed.")
    except (ConnectionError, ProtocolError, OSError):
        _shutdown(sock)
        raise
    if not ok:
        raise TransferError(message)
    return message


def download_file(sock, folder, filename):
    """Download one server file into folder. Returns the local path.

    Bytes are written to a temporary file first. The real file is replaced only
    after every byte has arrived, so a failed download does not leave a
    half-written file in place of a good one.
    """
    name = safe_filename(filename)
    dest = safe_join(folder, name)
    if os.path.lexists(dest) and not is_regular_file(dest):
        raise ValueError(f"Not a regular file: {name}")

    send_line(sock, f"DOWNLOAD {name}")
    ok, message = read_status(sock)
    if not ok:
        raise TransferError(message)
    if not message.isdigit():
        raise ProtocolError("Server sent an invalid file size.")
    size = int(message)
    if size > MAX_FILE_SIZE:
        raise ProtocolError("Server sent an invalid file size.")

    replaced = is_regular_file(dest)
    temp_path, fd = create_partial_file(folder)
    try:
        with os.fdopen(fd, "wb") as outfile:
            recv_file(sock, outfile, size)
            outfile.flush()
            os.fsync(outfile.fileno())
        os.replace(temp_path, dest)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return dest, size, replaced


def list_server_files(sock):
    """Ask the server for its files. Returns a list of (name, size) pairs."""
    send_line(sock, "LIST")
    ok, message = read_status(sock)
    if not ok:
        raise TransferError(message)
    if not message.isdigit():
        raise ProtocolError("Server sent an invalid file count.")
    count = int(message)
    if count > 100000:
        raise ProtocolError("Server sent an invalid file count.")
    entries = []
    for _ in range(count):
        line = recv_line(sock)
        if line is None:
            raise ConnectionError("Connection closed during LIST.")
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise ProtocolError("Server sent an invalid file list.")
        name = safe_filename(parts[0])
        entries.append((name, int(parts[1])))
    return entries


def print_menu():
    say("")
    say("Mini FTP Client")
    say("----------------")
    say("1. Upload file")
    say("2. Download file")
    say("3. List server files")
    say("4. Exit")


def _prompt(text):
    try:
        return input(text).strip()
    except EOFError:
        say("")
        return None
    except KeyboardInterrupt:
        say("")
        return None


def _print_entries(entries):
    for name, size in entries:
        say(f"  - {name} ({size} bytes)")


def do_upload(sock, folder):
    entries = local_files(folder)
    if not entries:
        say("No uploadable files were found in:")
        say(f"  {folder}")
        say("Put a file in that folder and try again.")
        return
    say("Files you can upload:")
    _print_entries(entries)
    filename = _prompt("File name to upload: ")
    if filename is None:
        raise KeyboardInterrupt
    if not filename:
        say("Error: file name cannot be empty.")
        return
    name = safe_filename(filename)
    path = safe_join(folder, name)
    if not os.path.lexists(path):
        raise FileNotFoundError(f"Local file not found: {name}")
    if not is_regular_file(path):
        raise ValueError(f"Not a regular file: {name}")
    size = os.lstat(path).st_size
    say(f"Uploading {name} ({size} bytes)...")
    message = upload_file(sock, folder, name)
    say(f"Success: {message}")


def do_download(sock, folder):
    filename = _prompt("File name to download: ")
    if filename is None:
        raise KeyboardInterrupt
    if not filename:
        say("Error: file name cannot be empty.")
        return
    name = safe_filename(filename)
    say(f"Downloading {name}...")
    dest, size, replaced = download_file(sock, folder, name)
    note = " (replaced existing file)" if replaced else ""
    say(f"Success: Downloaded {name} ({size} bytes) to {dest}{note}")


def do_list(sock):
    entries = list_server_files(sock)
    say(f"Success: {len(entries)} file(s) on the server.")
    if not entries:
        say("No files on the server.")
        return
    _print_entries(entries)


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def run_menu(sock, folder):
    """Show the menu until the user exits or the connection fails."""
    alive = True
    try:
        while True:
            print_menu()
            choice = _prompt("Choose an option (1-4): ")
            if choice is None:
                say("Goodbye.")
                break
            try:
                if choice == "1":
                    do_upload(sock, folder)
                elif choice == "2":
                    do_download(sock, folder)
                elif choice == "3":
                    do_list(sock)
                elif choice == "4":
                    say("Goodbye.")
                    break
                else:
                    say("Error: Please enter 1, 2, 3, or 4.")
            except (EOFError, KeyboardInterrupt):
                say("")
                say("Goodbye.")
                break
            except TransferError as exc:
                say(f"Error: {exc}")
            except FileNotFoundError as exc:
                # FileNotFoundError is a kind of OSError, so it has to be
                # handled before the connection errors below.
                say(f"Error: {exc}")
            except ValueError as exc:
                say(f"Error: {exc}")
            except (ProtocolError, ConnectionError, socket.timeout, TimeoutError, OSError) as exc:
                say(f"Error: {exc}")
                say("The connection was closed. Start the client again if you want to retry.")
                alive = False
                break
    finally:
        if alive:
            disconnect(sock)
        else:
            try:
                sock.close()
            except OSError:
                pass


def print_usage():
    say("Usage: python3 client.py [host] [port] [client_folder]")
    say(f"Default host: {HOST}")
    say(f"Default port: {PORT}")
    say(f"Default folder: {CLIENT_DIR}")
    say("Example: python3 client.py")
    say("Example: python3 client.py 127.0.0.1 5002")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print_usage()
        return 0
    try:
        host = HOST if not args else args[0]
        port = PORT if len(args) < 2 else parse_port(args[1])
        folder = CLIENT_DIR if len(args) < 3 else args[2]
    except ValueError as exc:
        say(f"Error: {exc}")
        print_usage()
        return 1
    if len(args) > 3:
        say("Error: too many arguments.")
        print_usage()
        return 1
    if not host:
        say("Error: host cannot be empty.")
        return 1

    os.makedirs(folder, exist_ok=True)
    cleanup_partial_files(folder)
    try:
        sock = connect_to_server(host, port)
    except ConnectionRefusedError:
        say(f"Error: could not connect to {host}:{port}.")
        say("Start the server first in another terminal: python3 server.py")
        return 1
    except socket.timeout:
        say(f"Error: timed out connecting to {host}:{port}.")
        return 1
    except OSError as exc:
        say(f"Error: could not connect to {host}:{port}: {exc.strerror or exc}")
        return 1

    say(f"Connected to {host}:{port}.")
    say(f"Local folder: {os.path.abspath(folder)}")
    try:
        run_menu(sock, folder)
    except KeyboardInterrupt:
        say("")
        say("Goodbye.")
        disconnect(sock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
