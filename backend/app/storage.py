"""Private object storage. Keys are internal identifiers, never public capabilities."""

import hashlib
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from app.config import get_settings


def validate_key(key: str) -> str:
    parts = PurePosixPath(key).parts
    if (
        not parts
        or key.startswith("/")
        or "\\" in key
        or ":" in key
        or any(part in {".", ".."} for part in key.split("/"))
        or any(not part for part in key.split("/"))
    ):
        raise ValueError("Invalid storage key")
    return key


class StorageProvider(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def content_hash(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def get_url(self, key: str) -> str: ...

    def list_prefix(self, prefix: str) -> list[str]: ...

    def delete_prefix(self, prefix: str) -> None: ...


class LocalStorageProvider:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid storage key")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def content_hash(self, key: str) -> str:
        with self._path(key).open("rb") as stream:
            return streaming_sha256(stream)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def get_url(self, key: str) -> str:
        # Internal locator only. API authorizes Page IDs before reading objects.
        return f"private://{validate_key(key)}"

    def list_prefix(self, prefix: str) -> list[str]:
        validate_key(prefix.rstrip("/"))
        root = (self.root / prefix).resolve()
        if not root.is_relative_to(self.root):
            raise ValueError("Invalid storage prefix")
        if not root.is_dir():
            return []
        return [
            path.relative_to(self.root).as_posix() for path in root.rglob("*") if path.is_file()
        ]

    def delete_prefix(self, prefix: str) -> None:
        for key in self.list_prefix(prefix):
            self.delete(key)


class S3StorageProvider:
    def __init__(self):
        settings = get_settings()
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name=settings.s3_region,
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=validate_key(key), Body=data, ContentType=content_type
        )

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=validate_key(key))
        with response["Body"] as body:
            return body.read()

    def content_hash(self, key: str) -> str:
        # ETags are not content hashes for multipart/encrypted objects.
        response = self.client.get_object(Bucket=self.bucket, Key=validate_key(key))
        with response["Body"] as body:
            return streaming_sha256(body)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=validate_key(key))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=validate_key(key))
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey"}:
                return False
            raise

    def get_url(self, key: str) -> str:
        return f"private://{validate_key(key)}"

    def list_prefix(self, prefix: str) -> list[str]:
        validate_key(prefix.rstrip("/"))
        response = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        )
        return [item["Key"] for page in response for item in page.get("Contents", [])]

    def delete_prefix(self, prefix: str) -> None:
        keys = self.list_prefix(prefix)
        for start in range(0, len(keys), 1000):
            result = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]]},
            )
            if result.get("Errors"):
                raise RuntimeError("Attempt object cleanup incomplete")


def streaming_sha256(stream) -> str:
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


class MinioStorageProvider(S3StorageProvider):
    """MinIO uses the same private S3 protocol."""


def get_storage() -> StorageProvider:
    settings = get_settings()
    if settings.storage_backend == "local":
        return LocalStorageProvider(settings.local_storage_path)
    if settings.storage_backend == "minio":
        return MinioStorageProvider()
    return S3StorageProvider()
