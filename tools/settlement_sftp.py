"""Strict-host-key SFTP publication of the U15 reference batch, local hosts only."""

import os
from pathlib import Path
import posixpath
import stat
import uuid

import paramiko

from tools.settlement import MAX_FILE_BYTES, payloads


class SftpSink:
    def __init__(
        self,
        host,
        port,
        username,
        private_key_file,
        known_hosts_file,
        root,
        passphrase_env=None,
    ):
        if host not in {"localhost", "127.0.0.1", "::1"} or not 0 < int(port) < 65536:
            raise ValueError("Reference SFTP is local only")
        if (
            not root.startswith("/")
            or ".." in root.split("/")
            or any(ord(c) < 32 for c in root)
        ):
            raise ValueError("Explicit absolute remote root required")
        self.host, self.port, self.username = host, int(port), username
        self.key, self.known_hosts = Path(private_key_file), Path(known_hosts_file)
        if not self.known_hosts.is_file() or self.key.stat().st_mode & 0o077:
            raise ValueError("Require pinned known_hosts and a private 0600 key file")
        self.root = posixpath.normpath(root)
        self.passphrase = (
            os.environ[passphrase_env].encode() if passphrase_env else None
        )
        self.destination = f"sftp://{username}@{host}:{self.port}{self.root}"

    @staticmethod
    def exists(sftp, path):
        try:
            return sftp.stat(path)
        except OSError as error:
            if error.errno == 2:
                return None
            raise

    @staticmethod
    def verify(sftp, target, expected):
        if set(sftp.listdir(target)) != set(expected):
            raise ValueError("Remote batch has unexpected/missing files")
        for name, data in expected.items():
            path = posixpath.join(target, name)
            if not stat.S_ISREG(sftp.lstat(path).st_mode):
                raise ValueError("Remote batch must contain regular files only")
            with sftp.open(path, "rb") as source:
                if source.read(MAX_FILE_BYTES + 1) != data:
                    raise ValueError("Remote batch conflicts with sealed content")

    def publish(self, batch):
        expected = payloads(batch)
        with paramiko.SSHClient() as client:
            client.load_host_keys(str(self.known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            key = paramiko.PKey.from_path(self.key, password=self.passphrase)
            client.connect(
                self.host,
                port=self.port,
                username=self.username,
                pkey=key,
                allow_agent=False,
                look_for_keys=False,
                timeout=5,
                banner_timeout=5,
                auth_timeout=5,
                channel_timeout=5,
            )
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(5)
                root = sftp.lstat(self.root)
                if not stat.S_ISDIR(root.st_mode):
                    raise ValueError("Remote root must already exist")
                target = posixpath.join(self.root, batch["id"])
                if not self.exists(sftp, target):
                    staging = posixpath.join(self.root, ".upload-" + uuid.uuid4().hex)
                    sftp.mkdir(staging, 0o700)
                    try:
                        for name, data in expected.items():
                            with sftp.open(posixpath.join(staging, name), "wx") as out:
                                out.write(data)
                                out.flush()
                        self.verify(sftp, staging, expected)
                        try:
                            sftp.rename(staging, target)
                        except OSError:
                            if not self.exists(sftp, target):
                                raise
                    finally:
                        if self.exists(sftp, staging):
                            for name in sftp.listdir(staging):
                                sftp.remove(posixpath.join(staging, name))
                            sftp.rmdir(staging)
                self.verify(sftp, target, expected)
        return dict(
            batch_id=batch["id"],
            manifest_sha256=batch["manifest_sha256"],
            transport="sftp",
            financial_acceptance=False,
        )
