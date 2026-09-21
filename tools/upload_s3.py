"""Fast parallel upload of a local folder to S3.

Uploads with many concurrent threads (default 48 — far above aws cli's
default of 10, which is what makes this faster for thousands of small PDFs).
Resumable: files already in the bucket with the same size are skipped, so
rerunning after an interruption only uploads what's missing.

Credentials come from the normal AWS chain (~/.aws/credentials, env vars).

Usage:
    uv run tools/upload_s3.py papers/ICML.cc_2026_Conference \
        s3://research-finder-2026/papers/ICML.cc_2026_Conference
    uv run tools/upload_s3.py papers s3://research-finder-2026/papers
    uv run tools/upload_s3.py papers s3://... --workers 64 --dry-run
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

# The outer thread pool provides the parallelism; keep per-file transfers
# single-threaded so the thread count stays predictable. Files above the
# multipart threshold (rare for papers) still use multipart, sequentially.
TRANSFER_CONFIG = TransferConfig(use_threads=False)


def parse_s3_url(url: str) -> tuple[str, str]:
    if not url.startswith("s3://"):
        sys.exit(f"Destination must look like s3://bucket/prefix, got: {url}")
    bucket, _, prefix = url[5:].partition("/")
    return bucket, prefix.strip("/")


def existing_keys(client, bucket: str, prefix: str) -> dict[str, int]:
    """key -> size for everything already under the prefix."""
    found = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            found[obj["Key"]] = obj["Size"]
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("local_dir", help="Local folder to upload")
    parser.add_argument("s3_url", help="Destination, e.g. s3://bucket/prefix")
    parser.add_argument("--workers", type=int, default=48,
                        help="Concurrent uploads (default: 48)")
    parser.add_argument("--profile", default=None,
                        help="AWS profile from ~/.aws/credentials "
                             "(default: the normal credential chain)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only report what would be uploaded")
    args = parser.parse_args()

    local_dir = Path(args.local_dir)
    if not local_dir.is_dir():
        sys.exit(f"Not a folder: {local_dir}")
    bucket, prefix = parse_s3_url(args.s3_url)

    # One client shared by all threads (boto3 clients are thread-safe);
    # pool size must cover the worker count or uploads serialize on
    # connection checkout.
    session = boto3.Session(profile_name=args.profile)
    client = session.client("s3", config=Config(
        max_pool_connections=max(args.workers + 8, 10),
        retries={"max_attempts": 5, "mode": "adaptive"}))

    files = sorted(f for f in local_dir.rglob("*") if f.is_file())
    total_bytes = sum(f.stat().st_size for f in files)
    print(f"{len(files)} files, {total_bytes / 1e9:.1f} GB under {local_dir}")

    print("Listing what's already in the bucket...")
    already = existing_keys(client, bucket, prefix)

    pending = []
    for f in files:
        key = f"{prefix}/{f.relative_to(local_dir)}" if prefix else \
            str(f.relative_to(local_dir))
        if already.get(key) != f.stat().st_size:
            pending.append((f, key))
    skipped = len(files) - len(pending)
    pending_bytes = sum(f.stat().st_size for f, _ in pending)
    print(f"{skipped} already uploaded; {len(pending)} to go "
          f"({pending_bytes / 1e9:.1f} GB).")

    if args.dry_run or not pending:
        if args.dry_run:
            for f, key in pending[:20]:
                print(f"  would upload: {key}")
            if len(pending) > 20:
                print(f"  ... and {len(pending) - 20} more")
        return

    progress = {"files": 0, "bytes": 0, "last_print": 0.0}
    lock = threading.Lock()
    start = time.time()

    def upload(f: Path, key: str) -> None:
        client.upload_file(str(f), bucket, key, Config=TRANSFER_CONFIG)
        size = f.stat().st_size
        with lock:
            progress["files"] += 1
            progress["bytes"] += size
            n = progress["files"]
            now = time.time()
            if now - progress["last_print"] >= 5 or n == len(pending):
                progress["last_print"] = now
                elapsed = now - start
                rate = progress["bytes"] / 1e6 / max(elapsed, 1e-9)
                eta = (pending_bytes - progress["bytes"]) / 1e6 / max(rate, 1e-9)
                print(f"[{n}/{len(pending)}] {progress['bytes'] / 1e9:.2f} GB "
                      f"at {rate:.0f} MB/s, ~{eta / 60:.0f} min left",
                      flush=True)

    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(upload, f, key): (f, key) for f, key in pending}
        for future in as_completed(futures):
            f, key = futures[future]
            try:
                future.result()
            except Exception as e:
                failed.append(key)
                print(f"FAILED ({e}): {key}")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed / 60:.1f} min: {len(pending) - len(failed)} "
          f"uploaded, {len(failed)} failed, {skipped} skipped.")
    if failed:
        print("Rerun the same command to retry the failed files.")
        sys.exit(1)


if __name__ == "__main__":
    main()
