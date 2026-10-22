"""Mini FTP server.

Listens on 127.0.0.1 port 5001 and stores files in server_files/.
Each client connection is handled on its own thread.

Run:
    python3 server.py
    python3 server.py 5001
    python3 server.py 5001 server_files
"""

import os
import socket
import stat
import sys
import threading
import time

from protocol import (
    MAX_FILE_SIZE,
    SOCKET_TIMEOUT,
    ProtocolError,
    cleanup_partial_files,
    create_partial_file,
    is_regular_file,
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
SERVER_DIR = os.path.join(BASE_DIR, "server_files")


def say(message):
    print(message, flush=True)


def parse_port(value, allow_zero=False):
    text = str(value)
    if not text.isdigit():
        raise ValueError(f"invalid port: {value}")
    port = int(text)
    low = 0 if allow_zero else 1
    if port < low or port > 65535:
        raise ValueError(f"port must be between {low} and 65535")
    return port


def parse_command(line):
    """Turn one request line into a command tuple, or raise ValueError."""
    parts = line.strip().split()
    if not parts:
        raise ValueError("Invalid command. Use LIST, UPLOAD, DOWNLOAD, or QUIT.")
    command = parts[0].upper()
    if command == "LIST" and len(parts) == 1:
        return ("LIST",)
    if command == "QUIT" and len(parts) == 1:
        return ("QUIT",)
    if command == "DOWNLOAD" and len(parts) == 2:
        return ("DOWNLOAD", safe_filename(parts[1]))
    if command == "UPLOAD" and len(parts) == 3:
        name = safe_filename(parts[1])
        size_text = parts[2]
        if not size_text.isdigit():
            raise ValueError("File size must be a non-negative whole number.")
        size = int(size_text)
        if size > MAX_FILE_SIZE:
            raise ValueError(
                f"File is too large. The maximum is {MAX_FILE_SIZE} bytes."
            )
        return ("UPLOAD", name, size)
    if command in ("LIST", "QUIT", "DOWNLOAD", "UPLOAD"):
        raise ValueError(f"Invalid {command} command.")
    raise ValueError("Unknown command. Use LIST, UPLOAD, DOWNLOAD, or QUIT.")


def list_storage(directory):
    """Return sorted (name, size) pairs for regular files in the storage folder."""
    entries = []
    for name in os.listdir(directory):
        try:
            safe_filename(name)
        except ValueError:
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        entries.append((name, info.st_size))
    entries.sort(key=lambda item: item[0].lower())
    return entries


def format_addr(addr):
    return f"{addr[0]}:{addr[1]}"


class MiniFTPServer:
    """TCP server that accepts many clients and serves the Mini FTP commands."""

    def __init__(self, host, port, storage_dir):
        self.host = host
        self.port = port
        self.storage_dir = storage_dir
        self._listen_sock = None
        self._thread = None
        self._running = False
        self._stopped = False
        self._lock = threading.Lock()
        self._clients = set()
        self._print_lock = threading.Lock()

    def log(self, message):
        with self._print_lock:
            say(message)

    def start(self):
        """Create the storage folder, bind the TCP port, and start accepting."""
        os.makedirs(self.storage_dir, exist_ok=True)
        cleanup_partial_files(self.storage_dir)
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Let a restarted server bind again without waiting for TIME_WAIT.
            listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listen_sock.bind((self.host, self.port))
            listen_sock.listen(16)
            # Wake up regularly so shutdown does not wait forever in accept().
            listen_sock.settimeout(0.5)
        except OSError as exc:
            listen_sock.close()
            detail = exc.strerror or str(exc)
            raise OSError(
                f"Could not listen on {self.host}:{self.port}. {detail}. "
                "If this port is already in use, stop the other program or run: "
                "python3 server.py 5002"
            ) from exc
        self._listen_sock = listen_sock
        self.port = listen_sock.getsockname()[1]
        self._running = True
        self._stopped = False
        self._thread = threading.Thread(
            target=self._accept_loop,
            name="miniftp-accept",
            daemon=True,
        )
        self._thread.start()

    def is_running(self):
        return self._running

    def shutdown(self):
        """Stop accepting clients and close current connections."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._running = False
            clients = list(self._clients)
        listen_sock = self._listen_sock
        self._listen_sock = None
        if listen_sock is not None:
            try:
                listen_sock.close()
            except OSError:
                pass
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(
                target=self._handle_client,
                args=(conn, addr),
                name=f"miniftp-client-{addr[1]}",
                daemon=True,
            )
            worker.start()

    def _handle_client(self, conn, addr):
        with self._lock:
            if not self._running:
                conn.close()
                return
            self._clients.add(conn)
        peer = format_addr(addr)
        self.log(f"[{peer}] connected")
        try:
            conn.settimeout(SOCKET_TIMEOUT)
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self._serve_client(conn, addr, peer)
        except socket.timeout:
            self.log(f"[{peer}] timed out")
            _try_send_line(conn, "ERR Timed out waiting for data.")
        except (ConnectionError, ProtocolError, OSError) as exc:
            self.log(f"[{peer}] connection closed: {exc}")
        except Exception as exc:
            self.log(f"[{peer}] unexpected error: {exc}")
        finally:
            with self._lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            self.log(f"[{peer}] disconnected")

    def _serve_client(self, conn, addr, peer):
        while self._running:
            try:
                line = recv_line(conn)
            except ProtocolError as exc:
                self.log(f"[{peer}] error: {exc}")
                _try_send_line(conn, f"ERR {exc}")
                break
            if line is None:
                break
            try:
                command = parse_command(line)
            except ValueError as exc:
                self.log(f"[{peer}] error: {exc}")
                send_line(conn, f"ERR {exc}")
                continue

            kind = command[0]
            if kind == "QUIT":
                self.log(f"[{peer}] QUIT")
                send_line(conn, "OK Goodbye")
                break
            if kind == "LIST":
                self.log(f"[{peer}] LIST")
                self._handle_list(conn)
            elif kind == "UPLOAD":
                _, name, size = command
                self.log(f"[{peer}] UPLOAD {name} ({size} bytes)")
                self._handle_upload(conn, peer, name, size)
            elif kind == "DOWNLOAD":
                _, name = command
                self.log(f"[{peer}] DOWNLOAD {name}")
                self._handle_download(conn, name)

    def _handle_list(self, conn):
        entries = list_storage(self.storage_dir)
        send_line(conn, f"OK {len(entries)}")
        for name, size in entries:
            send_line(conn, f"{name} {size}")

    def _handle_upload(self, conn, peer, name, size):
        try:
            dest = safe_join(self.storage_dir, name)
        except ValueError as exc:
            send_line(conn, f"ERR {exc}")
            return
        if os.path.lexists(dest) and not is_regular_file(dest):
            message = f"Not a regular file: {name}"
            self.log(f"[{peer}] error: {message}")
            send_line(conn, f"ERR {message}")
            return

        # Tell the client the command is valid before it sends file bytes.
        # Otherwise a rejected command and the file bytes would get mixed together.
        send_line(conn, "OK Ready")
        replaced = is_regular_file(dest)
        temp_path, fd = create_partial_file(self.storage_dir)
        try:
            with os.fdopen(fd, "wb") as outfile:
                recv_file(conn, outfile, size)
                outfile.flush()
                os.fsync(outfile.fileno())
            os.replace(temp_path, dest)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        if replaced:
            message = f"Replaced {name} ({size} bytes)"
        else:
            message = f"Uploaded {name} ({size} bytes)"
        send_line(conn, f"OK {message}")

    def _handle_download(self, conn, name):
        try:
            path = safe_join(self.storage_dir, name)
        except ValueError as exc:
            send_line(conn, f"ERR {exc}")
            return
        if not os.path.lexists(path) or not is_regular_file(path):
            if os.path.lexists(path):
                send_line(conn, f"ERR Not a regular file: {name}")
            else:
                send_line(conn, f"ERR File not found: {name}")
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError:
            send_line(conn, f"ERR File not found: {name}")
            return
        with os.fdopen(fd, "rb") as infile:
            info = os.fstat(infile.fileno())
            if not stat.S_ISREG(info.st_mode):
                send_line(conn, f"ERR Not a regular file: {name}")
                return
            size = info.st_size
            if size > MAX_FILE_SIZE:
                send_line(
                    conn,
                    f"ERR File is too large. The maximum is {MAX_FILE_SIZE} bytes.",
                )
                return
            send_line(conn, f"OK {size}")
            send_file(conn, infile, size)


def _try_send_line(sock, text):
    try:
        send_line(sock, text)
    except OSError:
        pass


def print_usage():
    say("Usage: python3 server.py [port] [storage_folder]")
    say(f"Default port: {PORT}")
    say(f"Default folder: {SERVER_DIR}")
    say("Example: python3 server.py")
    say("Example: python3 server.py 5002")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print_usage()
        return 0
    try:
        port = PORT if not args else parse_port(args[0], allow_zero=True)
        storage = SERVER_DIR if len(args) < 2 else args[1]
    except ValueError as exc:
        say(f"Error: {exc}")
        print_usage()
        return 1
    if len(args) > 2:
        say("Error: too many arguments.")
        print_usage()
        return 1

    server = MiniFTPServer(HOST, port, storage)
    try:
        server.start()
    except OSError as exc:
        say(f"Error: {exc}")
        return 1

    say(f"Mini FTP server listening on {HOST}:{server.port}")
    say(f"Storage folder: {os.path.abspath(storage)}")
    say("Press Ctrl+C to stop.")
    try:
        while server.is_running():
            time.sleep(0.3)
    except KeyboardInterrupt:
        say("\nStopping server...")
    finally:
        server.shutdown()
    say("Server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
