# Mini FTP Client–Server

Mini FTP is a small file-transfer program for a Computer Networks course. A Python client and a Python server talk over TCP. You can upload a file, download a file, and list the files stored on the server.

The protocol is a short custom design on port 5001. The goal is to make sockets, message framing, and file transfer easy to read. FileZilla and other internet FTP programs cannot connect to this server.

## Objectives

- Open a TCP connection between a client and a server.
- Send commands and file bytes over that connection.
- Keep commands and file data separate, even though TCP is a plain byte stream.
- Transfer text and binary files in chunks, without loading the whole file into memory.
- Handle bad commands, missing files, bad file names, and dropped connections safely.
- Let more than one client talk to the server at the same time.

## Features

- The server listens on **127.0.0.1 port 5001**.
- The client connects with a TCP socket and shows a numbered menu.
- Upload a file from `client_files/` to `server_files/`.
- Download a file from `server_files/` to `client_files/`.
- List the files stored on the server, with sizes.
- Clear success and error messages.
- Text, PDF, image, and other binary files all use the same path. The programs copy bytes and never try to decode the file.
- Files move in 64 KiB chunks.
- Several clients can be connected at once. Each client is served by its own thread.
- A transfer that stops halfway does not leave a half-written file, and it does not destroy the previous copy.

## Technologies

- Python 3 (this project was run with Python 3.9 on macOS)
- The standard-library `socket` module
- The standard-library `threading` module
- `unittest` for the automated tests

No third-party packages and no FTP library are required. Do not run `pip install`.

## Architecture

```
Terminal 1                         Terminal 2
server.py                          client.py
   |                                  |
   |  TCP  127.0.0.1:5001             |
   |  one command per line            |
   |  then the raw file bytes         |
   |                                  |
server_files/                    client_files/
```

1. `server.py` creates an IPv4 TCP socket (`AF_INET`, `SOCK_STREAM`), binds it to `127.0.0.1:5001`, and calls `listen`.
2. The main thread calls `accept`. Every accepted connection is passed to a new thread.
3. That thread reads one command line at a time. For an upload or download it then reads or writes exactly the number of file bytes announced in the command.
4. `client.py` connects, prints the menu, and sends one command when you choose an item.
5. Upload and download only touch the two storage folders. A file name cannot contain a folder path.

The server listens on `127.0.0.1` only, so other computers cannot connect. You can still open as many clients as you like on the same computer.

## Folder structure

```
Mini-FTP/
├── server.py           # TCP server
├── client.py           # menu-driven TCP client
├── protocol.py         # shared framing, file-name checks, and chunk helpers
├── test_mini_ftp.py    # unittest suite
├── README.md
├── client_files/       # files the client can upload; sample.txt is included
│   └── sample.txt
└── server_files/       # files uploaded to the server; starts with no listed files
```

`protocol.py` holds the rules both sides must share: how to read one line, how to read an exact number of bytes, and which file names are allowed. `client_files/` and `server_files/` are created automatically if you delete them.

`server_files/.gitkeep` is an empty placeholder so the folder can be stored in git. The server ignores names that start with a dot, so it does not appear in the file list.

## How TCP sockets are used

TCP gives each side a stream of bytes. It does not deliver “one command” or “one file” as one `recv` call.

- One `send` can arrive as several `recv` results.
- Several `send` calls can arrive in a single `recv`.
- A file can contain any byte, including newlines and zeros, so the receiver cannot search for a special marker to find the end of the file.

Mini FTP handles that in two steps.

**Commands and replies** are one UTF-8 line ending in `\n`. The reader calls `recv(1)` until it sees that newline. Short command lines make this easy to follow, and it avoids saving extra bytes that actually belong to the file that comes next. `sendall` is used for every line so a short write cannot drop the end of a command.

**File bytes are not a line.** The command tells the receiver the exact size first. After the reply, the sender writes the file in chunks of 64 KiB (`CHUNK_SIZE` in `protocol.py`). The receiver keeps calling `recv` until that many bytes have arrived, then stops. The next bytes on the socket are a new command line.

Upload uses one extra reply so a rejected command cannot get mixed up with file bytes:

1. The client sends `UPLOAD name size`.
2. The server answers `OK Ready`, or `ERR` plus a reason.
3. Only after `OK Ready` does the client send the raw bytes.
4. The server answers `OK Uploaded ...` or `OK Replaced ...`.

Each blocking socket call uses a 5-minute timeout (`SOCKET_TIMEOUT`). A large transfer is allowed to take longer than 5 minutes, because the timer starts again on every `recv` as long as data keeps arriving. A peer that goes silent is disconnected. The listening socket uses a short timeout only so Ctrl+C can stop the server promptly.

## Protocol

The client sends the first line. The server does not send a greeting by itself.

| Action | Client sends | Server replies | Then |
| --- | --- | --- | --- |
| List | `LIST` | `OK <count>` | one line per file: `<name> <size>` |
| Upload | `UPLOAD <name> <size>` | `OK Ready` or `ERR <reason>` | client sends exactly `<size>` raw bytes, then the server replies `OK Uploaded ...` or `OK Replaced ...` |
| Download | `DOWNLOAD <name>` | `OK <size>` or `ERR <reason>` | server sends exactly `<size>` raw bytes |
| Quit | `QUIT` | `OK Goodbye` | both sides close |

Example upload of the included sample:

```
UPLOAD sample.txt 79
OK Ready
<79 raw bytes>
OK Uploaded sample.txt (79 bytes)
```

An error looks like this:

```
ERR File not found: nosuchfile.txt
```

Unknown commands, bad sizes, and illegal file names get an `ERR` line, and the connection stays open. If the connection breaks in the middle of the raw file bytes, the receiver deletes its temporary file and closes. It does not send a text error in the middle of those bytes, because that text would be mixed into the file.

A file name may contain letters, numbers, dots, underscores, and hyphens. It must start with a letter or number. Spaces, slashes, and `..` are rejected.

Limits, all in `protocol.py`:

- Maximum file size: 100 MB (`MAX_FILE_SIZE`, 104857600 bytes)
- Maximum command line: 4096 characters (`MAX_LINE_LENGTH`)
- Chunk size: 64 KiB (`CHUNK_SIZE`)

## Setup on macOS

1. Open Terminal.
2. Check Python 3:

   ```bash
   python3 --version
   ```

   You need Python 3.9 or newer. macOS includes `python3`. You can also install it from [python.org](https://www.python.org/downloads/).

3. Go to the project folder:

   ```bash
   cd path/to/Mini-FTP
   ```

   Replace `path/to/Mini-FTP` with the folder where you saved or cloned this project.

There is nothing to install.

## Run the server

In the first terminal:

```bash
cd path/to/Mini-FTP
python3 server.py
```

Leave this terminal open. The server keeps running until you press Ctrl+C.

Optional forms:

```bash
python3 server.py 5002
python3 server.py 5001 /path/to/another/folder
```

The first optional argument is the port. The second is the folder where uploads are saved. The defaults are port `5001` and `server_files/` next to `server.py`.

## Run the client

Open a **second** terminal:

```bash
cd path/to/Mini-FTP
python3 client.py
```

The menu is:

```
1. Upload file
2. Download file
3. List server files
4. Exit
```

Type a number and press Return. When a command asks for a file name, type only the name, such as `sample.txt`. Do not type a folder, and do not type quotes.

To use another port:

```bash
python3 client.py 127.0.0.1 5002
```

You can start another client in a third terminal while the first client is still open. Each one gets its own connection.

## Testing uploads and downloads

`client_files/sample.txt` is already there. It is 79 bytes. `server_files/` starts with no listed files.

1. Start the server, then start the client.
2. Choose `3`. The server should report that it has no files.
3. Choose `1` and enter `sample.txt`.
4. Choose `3` again. `sample.txt` should now be listed as 79 bytes.
5. Confirm the file is on disk:

   ```bash
   ls -l server_files
   ```

6. Choose `2` and enter `sample.txt`. The client saves it back into `client_files/`. Because `sample.txt` was already there, the message says the local file was replaced. The old bytes stay untouched until the new copy has arrived in full.
7. Compare the two copies:

   ```bash
   shasum -a 256 client_files/sample.txt server_files/sample.txt
   ```

   Both hashes should be:

   ```
   766d6d52edb795bc0a77ebebe723c768896016bacae5331f94ea6971719bb96d
   ```

8. Choose `2` and enter `nosuchfile.txt`. The client should print `Error: File not found: nosuchfile.txt` and should not create that file locally.
9. Choose `1` and enter `../secret.txt`. The client should reject it before sending a file.
10. To test a PDF, image, or other binary file, copy it into `client_files/` and upload it by its exact file name. Or create a small binary file:

    ```bash
    python3 -c "open('client_files/picture.bin','wb').write(bytes([0,255,10,13,127,0])*1000)"
    ```

    Upload `picture.bin`, download it, then compare hashes the same way. The bytes are copied unchanged. A file larger than 64 KiB is still sent in chunks.

11. Choose `4` to disconnect. Press Ctrl+C in the server terminal to stop listening.

A successful upload or download replaces an existing regular file of the same name. The program writes to a temporary file in the same folder, then renames it into place only after every byte has been stored. If the connection drops halfway, the old file is unchanged and the temporary file is deleted.

## Expected output

Server terminal:

```
Mini FTP server listening on 127.0.0.1:5001
Storage folder: /path/to/Mini-FTP/server_files
Press Ctrl+C to stop.
[127.0.0.1:51810] connected
[127.0.0.1:51810] LIST
[127.0.0.1:51810] UPLOAD sample.txt (79 bytes)
[127.0.0.1:51810] LIST
[127.0.0.1:51810] DOWNLOAD sample.txt
[127.0.0.1:51810] DOWNLOAD nosuchfile.txt
[127.0.0.1:51810] QUIT
[127.0.0.1:51810] disconnected
```

The number after `127.0.0.1:` is the client’s temporary port. It changes every time a client connects. The storage path is the real path of `server_files` on that computer. Press Ctrl+C and the server prints:

```
Stopping server...
Server stopped.
```

Client terminal. The characters after each colon are what you type.

```
Connected to 127.0.0.1:5001.
Local folder: /path/to/Mini-FTP/client_files

Mini FTP Client
----------------
1. Upload file
2. Download file
3. List server files
4. Exit
Choose an option (1-4): 3
Success: 0 file(s) on the server.
No files on the server.

Mini FTP Client
----------------
1. Upload file
2. Download file
3. List server files
4. Exit
Choose an option (1-4): 1
Files you can upload:
  - sample.txt (79 bytes)
File name to upload: sample.txt
Uploading sample.txt (79 bytes)...
Success: Uploaded sample.txt (79 bytes)

Mini FTP Client
----------------
1. Upload file
2. Download file
3. List server files
4. Exit
Choose an option (1-4): 3
Success: 1 file(s) on the server.
  - sample.txt (79 bytes)

Mini FTP Client
----------------
1. Upload file
2. Download file
3. List server files
4. Exit
Choose an option (1-4): 2
File name to download: sample.txt
Downloading sample.txt...
Success: Downloaded sample.txt (79 bytes) to /path/to/Mini-FTP/client_files/sample.txt (replaced existing file)

Mini FTP Client
----------------
1. Upload file
2. Download file
3. List server files
4. Exit
Choose an option (1-4): 2
File name to download: nosuchfile.txt
Downloading nosuchfile.txt...
Error: File not found: nosuchfile.txt

Mini FTP Client
----------------
1. Upload file
2. Download file
3. List server files
4. Exit
Choose an option (1-4): 4
Goodbye.
```

If extra files are already in `client_files/`, the upload menu lists those too.

## Automated tests

From the project folder:

```bash
cd path/to/Mini-FTP
python3 -m unittest test_mini_ftp.py -v
```

The tests use a temporary folder and a free port, so they do not change `server_files/`. One test does bind port 5001 for a moment and then releases it. They check partial reads, text and binary round trips, an empty file, a missing file, a replaced file, a failed upload that must keep the old bytes, path traversal, a symbolic link that must not be followed, an unknown command, four clients at once, and the menu itself.

## Common errors and solutions

**`Error: could not connect to 127.0.0.1:5001.`**

The client was started first, or the server already exited. Start `python3 server.py` in another terminal, then start the client again.

**`Error: Could not listen on 127.0.0.1:5001. Address already in use.`**

Something is already listening on port 5001, often a server you started earlier. Find it and stop it:

```bash
lsof -nP -iTCP:5001 -sTCP:LISTEN
kill <PID>
```

Use the PID from the `lsof` line. You can also go to that server’s terminal and press Ctrl+C. Another option is to pick a free port in both terminals:

```bash
python3 server.py 5002
python3 client.py 127.0.0.1 5002
```

**`Error: File not found: nosuchfile.txt`**

That name is not a regular file in `server_files/`. Choose menu item `3` and copy the name exactly. This does not create a local file.

**`Error: Local file not found: ...`**

The name is not in `client_files/`. Put the file there first. The upload menu prints the names it can see.

**`Error: Invalid filename. Use a file name only, without a folder path.`**

Use `photo.png`, not `~/Desktop/photo.png`, `../photo.png`, or `folder/photo.png`. Copy the file into `client_files/` before uploading. Spaces are also rejected. Rename the file so the name uses letters, numbers, dots, underscores, and hyphens.

**`Error: File is too large. The maximum is 104857600 bytes.`**

The file is over 100 MB. The limit is `MAX_FILE_SIZE` in `protocol.py`.

**`The connection was closed. Start the client again if you want to retry.`**

The server stopped, or the other side sent no bytes for 5 minutes, during a transfer. Start the server again and run the client again. The previous complete file, if there was one, is still intact.

**`python3: command not found`**

Install Python 3, or try `python3.9` or `python3.11` if several versions are installed. Use that same command for both the server and the client.

## Limitations and security

This program is a teaching tool.

- There is no user name, password, or encryption. Anyone who can open the port can read and replace the files in `server_files/`.
- The server binds to `127.0.0.1` on purpose. Do not change `HOST` to `0.0.0.0`, and do not forward port 5001 to the internet.
- Do not store secrets in these folders, and do not run a file just because it was downloaded.
- File names are checked so a request cannot climb out of the storage folder with `..` or a slash. Symbolic links are not opened. That is not a full security review of a production file server.
- There are no subfolders, no resume of a partial transfer, and no rename command.
- File names cannot contain spaces.
- Only IPv4 is used.
- A finished transfer replaces a regular file of the same name. The rename is atomic on the same disk: a reader sees the old file or the new file, not a mixture. If two clients upload the same name at the same time, the upload that finishes last is the one that remains.
- An idle connection is closed after 5 minutes with no bytes.
- This server does not speak the internet FTP protocol, so real FTP clients cannot use it.
