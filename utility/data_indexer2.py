#!/usr/bin/env python3

import argparse
import hashlib
import statistics
from pathlib import Path

import fitz
import httpx
from loguru import logger

parser = argparse.ArgumentParser(description="Index documents from local file system")
parser.add_argument(
    "-u",
    "--url",
    default="http://localhost:8080",
    type=str,
    help="The base url of your OpenRAG instance",
)
parser.add_argument("-a", "--auth", required=False, type=str, help="AUTH_KEY (see the .env.example")
parser.add_argument(
    "-d",
    "--dir",
    required=True,
    type=str,
    help="The location of the documents to index",
)
parser.add_argument("-p", "--partition", required=True, type=str, help="Target partition")
args = parser.parse_args()

headers = {"accept": "application/json"}
if args.auth is not None and len(args.auth) > 0:
    headers["Authorization"] = f"Bearer {args.auth}"

dir_path = Path(args.dir).resolve()


def __check_api(base_url):
    try:
        response = httpx.get(f"{base_url}/health_check", headers=headers)
        if response.status_code == 200:
            logger.info("API is up and running")

    except httpx.RequestError as e:
        logger.debug(f"An error occurred: {e}")
        raise e


__check_api(args.url)

print(dir_path.is_dir())

limit = 100
i = 0
page_counts = []
file_sizes_mb = []

for file_path in dir_path.glob("**/*"):
    if file_path.is_file() and file_path.suffix.lower() == ".pdf":
        if i >= limit:
            logger.info(f"Reached the limit of {limit} files. Stopping.")
            break
        try:
            doc = fitz.open(file_path)
            page_count = doc.page_count
            doc.close()
        except Exception:
            page_count = 0
        file_size_mb = file_path.stat().st_size / (1024 * 1024)

        if page_count <= 10 or page_count > 40:
            # logger.info(f"Skipping {file_path.name} ({page_count} pages, {file_size_mb:.2f} MB)")
            continue

        page_counts.append(page_count)
        file_sizes_mb.append(file_size_mb)

        logger.info(f"file: {file_path} ({page_count} pages, {file_size_mb:.2f} MB)")
        file_ext = file_path.suffix[1:]  # Get the file extension without the dot
        filename = file_path.name  # Get the filename without the directory path

        absolute_path = str(file_path.resolve())
        file_id = hashlib.sha256(absolute_path.encode()).hexdigest()

        url = f"{args.url}/indexer/partition/{args.partition}/file/{file_id}"

        with open(file_path, "rb") as f:
            files = {
                "file": (filename, f, f"application/{file_ext}"),
                "metadata": (None, ""),
            }

            response = httpx.post(
                url,
                files=files,
                headers=headers,
                timeout=60,
            )
            i += 1
            print(f"Uploaded {filename}: {response.status_code} - {response.text}")

if page_counts:
    logger.info("--- Statistics (uploaded files) ---")
    logger.info(
        f"Pages  -> min: {min(page_counts)}, max: {max(page_counts)}, "
        f"mean: {statistics.mean(page_counts):.1f}, std: {statistics.stdev(page_counts) if len(page_counts) > 1 else 0:.1f}"
    )
    logger.info(
        f"Size (MB) -> min: {min(file_sizes_mb):.2f}, max: {max(file_sizes_mb):.2f}, "
        f"mean: {statistics.mean(file_sizes_mb):.2f}, std: {statistics.stdev(file_sizes_mb) if len(file_sizes_mb) > 1 else 0:.2f}"
    )
else:
    logger.info("No files were uploaded.")


# # How to run this code:
# uv run python utility/data_indexer2.py \
#     -d /path/to/your/documents \
#     -p your_partition_name \
#     -u http://localhost:8080 \
#     -a your_auth_key
