"""S3 artifact store: per-paper extraction artifacts under S3_ARTIFACTS_URL.

Layout (prefixes are paper_ids, mirroring the local markdown/ folders):

    s3://<bucket>/<prefix>/metadata.json          download metadata (all venues)
    s3://<bucket>/<prefix>/<paper_id>/01.md ...   per-page markdown
    s3://<bucket>/<prefix>/<paper_id>/paper.json  structured extraction
    s3://<bucket>/<prefix>/<paper_id>/summary.md  detailed summary

Auth is ambient AWS credentials: an EC2 instance role, or AWS_PROFILE /
~/.aws/credentials locally.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import boto3


class ArtifactStore:
    def __init__(self, url: str | None = None):
        url = url or os.environ.get("S3_ARTIFACTS_URL")
        if not url:
            raise RuntimeError("S3_ARTIFACTS_URL is not set (see .env.example)")
        parsed = urlparse(url)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise RuntimeError(f"S3_ARTIFACTS_URL must look like "
                               f"s3://bucket/prefix, got: {url}")
        self.bucket = parsed.netloc
        self.prefix = parsed.path.strip("/")
        session = boto3.Session(
            profile_name=os.environ.get("AWS_PROFILE") or None)
        self.s3 = session.client("s3")

    def key(self, *parts: str) -> str:
        return "/".join(p.strip("/") for p in (self.prefix, *parts) if p)

    def url(self, *parts: str) -> str:
        return f"s3://{self.bucket}/{self.key(*parts)}"

    def _list(self, prefix: str) -> list[str]:
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    def upload_folder(self, folder: Path, paper_id: str) -> int:
        """Upload every file in a paper's folder, skipping objects that
        already exist with the same size (idempotent resume)."""
        existing = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.key(paper_id) + "/"):
            for obj in page.get("Contents", []):
                existing[obj["Key"]] = obj["Size"]
        uploaded = 0
        for f in sorted(folder.iterdir()):
            if not f.is_file():
                continue
            key = self.key(paper_id, f.name)
            if existing.get(key) == f.stat().st_size:
                continue
            self.s3.upload_file(str(f), self.bucket, key)
            uploaded += 1
        return uploaded

    def upload_file(self, path: Path, *key_parts: str) -> None:
        self.s3.upload_file(str(path), self.bucket, self.key(*key_parts))

    def download_file(self, dest: Path, *key_parts: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, self.key(*key_parts), str(dest))

    def list_paper_jsons(self) -> list[tuple[str, str]]:
        """All (paper_id, key) pairs for <prefix>/<paper_id>/paper.json."""
        out = []
        for key in self._list(self.key() + "/" if self.key() else ""):
            if key.endswith("/paper.json"):
                out.append((key.split("/")[-2], key))
        return out

    def get_json(self, key: str) -> dict | list:
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        return json.loads(body.read())

    def exists(self, *key_parts: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.key(*key_parts))
            return True
        except self.s3.exceptions.ClientError:
            return False
