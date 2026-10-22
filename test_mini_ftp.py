"""Automated tests for the Mini FTP client and server.

Run from the project folder:

    python3 -m unittest test_mini_ftp.py -v
"""

import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import client
import protocol
import server

ROOT = os.path.dirname(os.path.abspath(__file__))
CLIENT_PY = os.path.join(ROOT, "client.py")


class FakeSocket:
    """Socket stand-in that returns preloaded pieces, like a split TCP stream."""

    def __init__(self, chunks):
        self._chunks = [bytearray(chunk) for chunk in chunks]

    def recv(self, size):
        if size <= 0:
            return b""
        while self._chunks and not self._chunks[0]:
            self._chunks.pop(0)
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        data = bytes(chunk[:size])
        del chunk[:size]
        return data


class ProtocolTests(unittest.TestCase):
    def test_recv_exact_accepts_partial_reads(self):
        sock = FakeSocket([b"he", b"l", b"lo!"])
        self.assertEqual(protocol.recv_exact(sock, 6), b"hello!")

    def test_recv_exact_reports_early_close(self):
        sock = FakeSocket([b"hi"])
        with self.assertRaises(ConnectionError):
            protocol.recv_exact(sock, 4)

    def test_recv_line_handles_split_packets_and_crlf(self):
        sock = FakeSocket([b"LI", b"ST\r", b"\n"])
        self.assertEqual(protocol.recv_line(sock), "LIST")

    def test_recv_line_clean_close(self):
        self.assertIsNone(protocol.recv_line(FakeSocket([])))

    def test_recv_file_writes_chunks_without_loading_one_giant_buffer(self):
        payload = bytes((index % 256) for index in range(protocol.CHUNK_SIZE + 25))
        pieces = [payload[:10], payload[10:100], payload[100:]]
        output = io.BytesIO()
        protocol.recv_file(FakeSocket(pieces), output, len(payload))
        self.assertEqual(output.getvalue(), payload)

    def test_send_and_recv_file_over_socketpair(self):
        payload = bytes((index * 3) % 256 for index in range(protocol.CHUNK_SIZE * 2 + 11))
        left, right = socket.socketpair()
        left.settimeout(5)
        right.settimeout(5)
        received = io.BytesIO()

        def read_side():
            protocol.recv_file(right, received, len(payload))
            right.close()

        reader = threading.Thread(target=read_side)
        reader.start()
        protocol.send_file(left, io.BytesIO(payload), len(payload))
        left.close()
        reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(received.getvalue(), payload)

    def test_safe_filename_rejects_traversal_and_odd_names(self):
        self.assertEqual(protocol.safe_filename("notes.txt"), "notes.txt")
        for bad in ("../secret", "/etc/passwd", "a/b.txt", "..", ".", "", "a b.txt", ".hidden"):
            with self.assertRaises(ValueError):
                protocol.safe_filename(bad)

    def test_parse_command_validates_names_and_sizes(self):
        self.assertEqual(server.parse_command("list"), ("LIST",))
        self.assertEqual(server.parse_command("UPLOAD photo.png 12"), ("UPLOAD", "photo.png", 12))
        with self.assertRaises(ValueError):
            server.parse_command("UPLOAD ../a.txt 3")
        with self.assertRaises(ValueError):
            server.parse_command("UPLOAD a.txt -1")
        with self.assertRaises(ValueError):
            server.parse_command(f"UPLOAD a.txt {protocol.MAX_FILE_SIZE + 1}")
        with self.assertRaises(ValueError):
            server.parse_command("NOPE")


class MiniFTPIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.storage = os.path.join(cls.tmp.name, "server_files")
        cls.client_dir = os.path.join(cls.tmp.name, "client_files")
        os.makedirs(cls.storage)
        os.makedirs(cls.client_dir)
        cls.server = server.MiniFTPServer("127.0.0.1", 0, cls.storage)
        cls.server.start()
        cls.port = cls.server.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        clear_directory(self.storage)
        clear_directory(self.client_dir)

    def connect(self):
        return client.connect_to_server("127.0.0.1", self.port)

    def test_text_upload_list_and_download(self):
        source = os.path.join(self.client_dir, "hello.txt")
        text = "Hello from Mini FTP!\nLine two.\n"
        with open(source, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        sock = self.connect()
        message = client.upload_file(sock, self.client_dir, "hello.txt")
        self.assertIn("Uploaded hello.txt", message)
        entries = client.list_server_files(sock)
        self.assertEqual(entries, [("hello.txt", len(text.encode("utf-8")))])
        os.remove(source)
        dest, size, replaced = client.download_file(sock, self.client_dir, "hello.txt")
        client.disconnect(sock)
        self.assertFalse(replaced)
        self.assertEqual(size, len(text.encode("utf-8")))
        with open(dest, "r", encoding="utf-8", newline="\n") as handle:
            self.assertEqual(handle.read(), text)
        with open(os.path.join(self.storage, "hello.txt"), "rb") as handle:
            stored = handle.read()
        with open(dest, "rb") as handle:
            self.assertEqual(handle.read(), stored)

    def test_binary_roundtrip_is_byte_for_byte(self):
        payload = bytes((index % 256) for index in range(protocol.CHUNK_SIZE + 1024))
        self.assertIn(0, payload)
        self.assertIn(255, payload)
        source = os.path.join(self.client_dir, "picture.bin")
        with open(source, "wb") as handle:
            handle.write(payload)
        sock = self.connect()
        client.upload_file(sock, self.client_dir, "picture.bin")
        downloaded, size, _replaced = client.download_file(
            sock, self.client_dir, "picture.bin"
        )
        client.disconnect(sock)
        with open(os.path.join(self.storage, "picture.bin"), "rb") as handle:
            stored = handle.read()
        with open(downloaded, "rb") as handle:
            local = handle.read()
        self.assertEqual(size, len(payload))
        self.assertEqual(stored, payload)
        self.assertEqual(local, payload)

    def test_download_missing_file_and_connection_still_works(self):
        sock = self.connect()
        with self.assertRaises(protocol.TransferError) as caught:
            client.download_file(sock, self.client_dir, "missing.txt")
        self.assertIn("File not found", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.client_dir, "missing.txt")))
        self.assertEqual(client.list_server_files(sock), [])
        client.disconnect(sock)

    def test_invalid_command_and_bad_upload_do_not_desync(self):
        sock = self.connect()
        sock.settimeout(5)
        protocol.send_line(sock, "HELLO")
        ok, message = protocol.read_status(sock)
        self.assertFalse(ok)
        self.assertIn("Unknown command", message)
        protocol.send_line(sock, "UPLOAD ../secret.txt 4")
        ok, message = protocol.read_status(sock)
        self.assertFalse(ok)
        protocol.send_line(sock, f"UPLOAD big.bin {protocol.MAX_FILE_SIZE + 1}")
        ok, message = protocol.read_status(sock)
        self.assertFalse(ok)
        self.assertIn("too large", message.lower())
        protocol.send_line(sock, "LIST")
        ok, message = protocol.read_status(sock)
        self.assertTrue(ok)
        self.assertEqual(message, "0")
        client.disconnect(sock)

    def test_interrupted_upload_does_not_corrupt_existing_file(self):
        dest = os.path.join(self.storage, "keep.txt")
        with open(dest, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("original-data")
        sock = self.connect()
        sock.settimeout(5)
        protocol.send_line(sock, "UPLOAD keep.txt 100")
        ok, message = protocol.read_status(sock)
        self.assertTrue(ok)
        self.assertEqual(message, "Ready")
        sock.sendall(b"abcde")
        sock.close()
        deadline = time.time() + 3
        while time.time() < deadline:
            partials = [name for name in os.listdir(self.storage) if name.startswith(".partial-")]
            if not partials:
                break
            time.sleep(0.05)
        with open(dest, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original-data")
        partials = [name for name in os.listdir(self.storage) if name.startswith(".partial-")]
        self.assertEqual(partials, [])
        sock = self.connect()
        self.assertEqual(client.list_server_files(sock), [("keep.txt", len("original-data"))])
        client.disconnect(sock)

    def test_replace_existing_file_after_full_upload(self):
        source = os.path.join(self.client_dir, "keep.txt")
        with open(source, "wb") as handle:
            handle.write(b"new-data")
        with open(os.path.join(self.storage, "keep.txt"), "wb") as handle:
            handle.write(b"old-data")
        sock = self.connect()
        message = client.upload_file(sock, self.client_dir, "keep.txt")
        downloaded, _size, _replaced = client.download_file(sock, self.client_dir, "keep.txt")
        client.disconnect(sock)
        self.assertIn("Replaced keep.txt", message)
        with open(downloaded, "rb") as handle:
            self.assertEqual(handle.read(), b"new-data")

    def test_empty_file_roundtrip(self):
        source = os.path.join(self.client_dir, "empty.dat")
        with open(source, "wb") as handle:
            handle.write(b"")
        sock = self.connect()
        client.upload_file(sock, self.client_dir, "empty.dat")
        os.remove(source)
        dest, size, _replaced = client.download_file(sock, self.client_dir, "empty.dat")
        client.disconnect(sock)
        self.assertEqual(size, 0)
        with open(dest, "rb") as handle:
            self.assertEqual(handle.read(), b"")

    def test_symlink_is_not_followed(self):
        secret = os.path.join(self.tmp.name, "secret.txt")
        with open(secret, "w", encoding="utf-8") as handle:
            handle.write("top-secret")
        link = os.path.join(self.storage, "link.txt")
        os.symlink(secret, link)
        sock = self.connect()
        with self.assertRaises(protocol.TransferError) as caught:
            client.download_file(sock, self.client_dir, "link.txt")
        self.assertIn("Not a regular file", str(caught.exception))
        source = os.path.join(self.client_dir, "link.txt")
        with open(source, "wb") as handle:
            handle.write(b"safe-data")
        with self.assertRaises(protocol.TransferError):
            client.upload_file(sock, self.client_dir, "link.txt")
        client.disconnect(sock)
        with open(secret, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "top-secret")
        self.assertTrue(os.path.islink(link))

    def test_client_rejects_path_traversal_locally(self):
        sock = self.connect()
        with self.assertRaises(ValueError):
            client.upload_file(sock, self.client_dir, "../server.py")
        self.assertEqual(client.list_server_files(sock), [])
        client.disconnect(sock)

    def test_concurrent_clients(self):
        errors = []
        barrier = threading.Barrier(4)

        def worker(index):
            try:
                name = f"part{index}.bin"
                payload = bytes((index + offset) % 256 for offset in range(50000))
                folder = os.path.join(self.tmp.name, f"client-{index}")
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, name), "wb") as handle:
                    handle.write(payload)
                barrier.wait(timeout=5)
                sock = self.connect()
                client.upload_file(sock, folder, name)
                dest, _size, _replaced = client.download_file(sock, folder, name)
                client.disconnect(sock)
                with open(dest, "rb") as handle:
                    if handle.read() != payload:
                        errors.append(f"{name} bytes did not match")
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        names = sorted(name for name, _size in server.list_storage(self.storage))
        self.assertEqual(names, ["part0.bin", "part1.bin", "part2.bin", "part3.bin"])

    def test_abrupt_disconnect_does_not_stop_the_server(self):
        sock = self.connect()
        sock.close()
        time.sleep(0.1)
        sock = self.connect()
        self.assertEqual(client.list_server_files(sock), [])
        client.disconnect(sock)

    def test_interactive_menu(self):
        sample = os.path.join(self.client_dir, "menu.txt")
        with open(sample, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("menu-test\n")
        script = "1\nmenu.txt\n3\n2\nmenu.txt\n2\nmissing.txt\n9\n4\n"
        completed = subprocess.run(
            [
                sys.executable,
                CLIENT_PY,
                "127.0.0.1",
                str(self.port),
                self.client_dir,
            ],
            input=script,
            text=True,
            capture_output=True,
            timeout=15,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + "\n" + completed.stderr,
        )
        output = completed.stdout
        self.assertIn("Connected to 127.0.0.1:", output)
        self.assertIn("Success: Uploaded menu.txt", output)
        self.assertIn("Success: 1 file(s) on the server.", output)
        self.assertIn("menu.txt", output)
        self.assertIn("Success: Downloaded menu.txt", output)
        self.assertIn("Error: File not found: missing.txt", output)
        self.assertIn("Error: Please enter 1, 2, 3, or 4.", output)
        self.assertIn("Goodbye.", output)
        with open(os.path.join(self.storage, "menu.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"menu-test\n")

    def test_default_port_constant_is_5001(self):
        self.assertEqual(server.PORT, 5001)
        self.assertEqual(client.PORT, 5001)


class LivePort5001Tests(unittest.TestCase):
    def test_server_listens_on_port_5001_and_cleans_partials(self):
        storage = tempfile.mkdtemp(prefix="miniftp-5001-")
        try:
            partial = os.path.join(storage, ".partial-old-transfer")
            with open(partial, "wb") as handle:
                handle.write(b"junk")
            ftp = server.MiniFTPServer(server.HOST, 5001, storage)
            try:
                ftp.start()
            except OSError as exc:
                self.fail(f"Could not listen on port 5001: {exc}")
            try:
                self.assertEqual(ftp.port, 5001)
                self.assertFalse(os.path.exists(partial))
                probe = socket.create_connection((server.HOST, 5001), timeout=5)
                probe.close()
                sock = client.connect_to_server(server.HOST, 5001)
                self.assertEqual(client.list_server_files(sock), [])
                client.disconnect(sock)
            finally:
                ftp.shutdown()
        finally:
            shutil.rmtree(storage, ignore_errors=True)


def clear_directory(path):
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.islink(full) or os.path.isfile(full):
            os.remove(full)
        elif os.path.isdir(full):
            os.rmdir(full)


if __name__ == "__main__":
    unittest.main()
