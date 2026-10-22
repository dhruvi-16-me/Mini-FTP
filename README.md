# Mini FTP Client–Server

A small Python file-transfer program for a Computer Networks course. The client and server talk over TCP on port 5001. You can upload a file, download a file, and list the files on the server.

This is a custom teaching protocol. It is not internet FTP, so FileZilla cannot connect to it. No extra packages are required.

## What it does

- The server listens on `127.0.0.1:5001` and handles each client on its own thread.
- The client menu uploads, downloads, or lists files.
- Text, PDF, image, and other binary files are copied as raw bytes in 64 KiB chunks.
- The receiver is told the exact file size, so it knows when the file ends.
- A failed transfer deletes its temporary file and leaves the previous copy unchanged.
- File names cannot contain a path (`../secret.txt` is rejected). Access stays inside `client_files/` and `server_files/`.

## Layout

```
Mini-FTP/
├── server.py           # TCP server
├── client.py           # menu client
├── protocol.py         # shared framing and filename checks
├── test_mini_ftp.py
├── client_files/       # files you can upload (includes sample.txt)
└── server_files/       # files stored on the server
```

Both folders are created automatically if they are missing.

## How TCP is used

TCP delivers a stream of bytes, not whole messages. One `send` may arrive in several `recv` calls.

Commands are one UTF-8 line ending in `\n`. File data comes after that line and is not treated as text. The sender announces the size, then both sides read or write until that many bytes have been transferred.

```
UPLOAD sample.txt 79
OK Ready
<79 raw bytes>
OK Uploaded sample.txt (79 bytes)
```

| Action | Client sends | Server replies | Then |
| --- | --- | --- | --- |
| List | `LIST` | `OK <count>` | one `<name> <size>` line per file |
| Upload | `UPLOAD <name> <size>` | `OK Ready` or `ERR <reason>` | exactly `<size>` raw bytes, then `OK Uploaded ...` |
| Download | `DOWNLOAD <name>` | `OK <size>` or `ERR <reason>` | exactly `<size>` raw bytes |
| Quit | `QUIT` | `OK Goodbye` | both sides close |

Names may use letters, numbers, `.`, `_`, and `-`. The maximum file size is 100 MB.

## Run it

Use Python 3.9 or newer. Open two terminals in the project folder (`path/to/Mini-FTP` means the folder where you cloned this project).

Terminal 1:

```bash
cd path/to/Mini-FTP
python3 server.py
```

Terminal 2:

```bash
cd path/to/Mini-FTP
python3 client.py
```

```
1. Upload file
2. Download file
3. List server files
4. Exit
```

Try this:

1. Choose `3`. The server has no files yet.
2. Choose `1` and enter `sample.txt`.
3. Choose `3` again. `sample.txt` is listed (79 bytes).
4. Choose `2` and enter `sample.txt` to download it.
5. Choose `2` and enter `nosuchfile.txt`. You should see `Error: File not found: nosuchfile.txt`.
6. Choose `4`. Press Ctrl+C in the server terminal to stop it.

Check that both copies match:

```bash
shasum -a 256 client_files/sample.txt server_files/sample.txt
```

To try a binary file, copy a PDF or image into `client_files/` and upload it by its file name. Another port works with `python3 server.py 5002` and `python3 client.py 127.0.0.1 5002`.

Server output looks like this. The port after `127.0.0.1:` changes for each connection, and the storage path is the real path on that computer.

```
Mini FTP server listening on 127.0.0.1:5001
Storage folder: /path/to/Mini-FTP/server_files
Press Ctrl+C to stop.
[127.0.0.1:51810] connected
[127.0.0.1:51810] UPLOAD sample.txt (79 bytes)
[127.0.0.1:51810] disconnected
```

## Tests

```bash
python3 -m unittest test_mini_ftp.py -v
```

