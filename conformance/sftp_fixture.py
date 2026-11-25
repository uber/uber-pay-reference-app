"""Loopback-only real SSH/SFTP test server, jailed to a temporary synthetic tree."""

import os
from pathlib import Path
import socket
import threading

import paramiko


class Identity(paramiko.ServerInterface):
    def __init__(self, key):
        self.key = key

    def check_auth_publickey(self, username, key):
        return (
            paramiko.AUTH_SUCCESSFUL
            if username == "reference" and key == self.key
            else paramiko.AUTH_FAILED
        )

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, *, root, fixture=None):
        super().__init__(server)
        self.root = Path(root).resolve()
        self.identity = server
        self.fixture = fixture

    def local(self, path):
        candidate = self.root / path.lstrip("/")
        candidate.resolve().relative_to(self.root)
        return candidate

    def call(self, fn):
        try:
            return fn()
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)
        except ValueError:
            return paramiko.SFTP_PERMISSION_DENIED

    def stat(self, path):
        return self.call(
            lambda: paramiko.SFTPAttributes.from_stat(self.local(path).stat())
        )

    def lstat(self, path):
        return self.call(
            lambda: paramiko.SFTPAttributes.from_stat(self.local(path).lstat())
        )

    def list_folder(self, path):
        def items():
            out = []
            for child in self.local(path).iterdir():
                value = paramiko.SFTPAttributes.from_stat(child.lstat())
                value.filename = child.name
                out.append(value)
            return out

        return self.call(items)

    def mkdir(self, path, attr):
        return self.call(
            lambda: (
                self.local(path).mkdir(mode=attr.st_mode or 0o700),
                paramiko.SFTP_OK,
            )[1]
        )

    def rmdir(self, path):
        return self.call(lambda: (self.local(path).rmdir(), paramiko.SFTP_OK)[1])

    def remove(self, path):
        return self.call(lambda: (self.local(path).unlink(), paramiko.SFTP_OK)[1])

    def rename(self, old, new):
        def move():
            if self.local(new).exists():
                raise FileExistsError(17, "Destination exists")
            self.local(old).rename(self.local(new))
            if self.fixture and self.fixture.drop_after_rename:
                self.fixture.drop_after_rename = False
                self.identity.transport.close()
            return paramiko.SFTP_OK

        return self.call(move)

    def open(self, path, flags, attr):
        def create():
            fd = os.open(self.local(path), flags, attr.st_mode or 0o600)
            mode = "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb"
            file = os.fdopen(fd, mode)
            handle = paramiko.SFTPHandle(flags)
            if not flags & os.O_WRONLY:
                handle.readfile = file
            if flags & (os.O_WRONLY | os.O_RDWR):
                handle.writefile = file
            return handle

        return self.call(create)


class SftpFixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "reports").mkdir(exist_ok=True)
        self.host_key = paramiko.RSAKey.generate(2048)
        self.client_key = paramiko.RSAKey.generate(2048)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.socket.listen()
        self.socket.settimeout(0.2)
        self.stopped = threading.Event()
        self.transports = []
        self.drop_after_rename = False
        self.thread = threading.Thread(target=self.run, daemon=True)

    def config(self):
        key = self.directory / "client.key"
        self.client_key.write_private_key_file(str(key))
        key.chmod(0o600)
        hosts = self.directory / "known_hosts"
        hosts.write_text(
            f"[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n"
        )
        return dict(
            host="127.0.0.1",
            port=self.port,
            username="reference",
            private_key_file=str(key),
            known_hosts_file=str(hosts),
            root="/reports",
        )

    def start(self):
        self.thread.start()
        return self

    def run(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
                transport = paramiko.Transport(connection)
                self.transports.append(transport)
                transport.add_server_key(self.host_key)
                transport.set_subsystem_handler(
                    "sftp",
                    paramiko.SFTPServer,
                    Files,
                    root=str(self.directory),
                    fixture=self,
                )
                identity = Identity(self.client_key)
                identity.transport = transport
                transport.start_server(server=identity)
            except (OSError, EOFError, paramiko.SSHException):
                if self.stopped.is_set():
                    return

    def close(self):
        self.stopped.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(timeout=2)
