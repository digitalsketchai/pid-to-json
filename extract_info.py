import base64
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import List

import requests
from dotenv import load_dotenv

SAMPLE_DIR = Path(__file__).parent / "sample"
OUTPUT_DIR = Path(__file__).parent / "info"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp"}
PDF_EXT = {".pdf"}
SUPPORTED_EXT = IMAGE_EXT | PDF_EXT

def get_config():
    load_dotenv()
    return {
        "pipeline_status_poll_interval": int(os.getenv("PIPELINE_STATUS_POLL_INTERVAL_SECONDS", "40")),
        "run_id_poll_interval": int(os.getenv("RUN_ID_POLL_INTERVAL_SECONDS", "15")),
        "pipeline_status_timeout": int(os.getenv("PIPELINE_STATUS_TIMEOUT_SECONDS", "7200")),
        "run_id_timeout": int(os.getenv("RUN_ID_TIMEOUT_SECONDS", "600")),
        "pdf_poll_interval": int(os.getenv("PDF_POLL_INTERVAL_SECONDS", "2")),
        "pdf_timeout": int(os.getenv("PDF_TIMEOUT_SECONDS", "900")),
    }

CONFIG = get_config()
PIPELINE_STATUS_POLL_INTERVAL_SECONDS = CONFIG["pipeline_status_poll_interval"]
PIPELINE_STATUS_TIMEOUT_SECONDS = CONFIG["pipeline_status_timeout"]
RUN_ID_POLL_INTERVAL_SECONDS = CONFIG["run_id_poll_interval"]
RUN_ID_TIMEOUT_SECONDS = CONFIG["run_id_timeout"]
PDF_POLL_INTERVAL_SECONDS = CONFIG["pdf_poll_interval"]
PDF_TIMEOUT_SECONDS = CONFIG["pdf_timeout"]

STATUS_TEXT = {
    -1: "queued",
    0: "running",
    1: "complete",
    2: "error",
    3: "stopped",
    4: "timeout",
    5: "unknown",
}


def pick_sample_file(filename: str = None) -> Path:
    if not SAMPLE_DIR.exists():
        raise FileNotFoundError(f"Sample directory not found: {SAMPLE_DIR}")

    if filename:
        filepath = SAMPLE_DIR / filename
        if filepath.exists() and filepath.is_file():
            return filepath
        raise FileNotFoundError(f"File not found: {filepath}")

    files = [
        p for p in sorted(SAMPLE_DIR.iterdir())
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT
    ]
    if not files:
        raise FileNotFoundError(
            f"No supported P&ID file found in {SAMPLE_DIR}. "
            f"Supported extensions: {sorted(SUPPORTED_EXT)}"
        )
    return files[0]


def encode_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def upload_image(base_url: str, api_key: str, image_path: Path) -> str:
    url = f"{base_url}/digitalsketch/uploadimage"
    print(f"  POST {url}")
    mime_type, _ = mimetypes.guess_type(image_path.name)
    body = {
        "api_key": api_key,
        "base64_image": encode_base64(image_path),
    }
    if mime_type:
        body["mime_type"] = mime_type
    resp = requests.post(url, json=body, timeout=180)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success") or not payload.get("imageid"):
        raise RuntimeError(f"Image upload failed: {payload}")
    return payload["imageid"]


def upload_pdf(base_url: str, api_key: str, pdf_path: Path) -> str:
    url = f"{base_url}/digitalsketch/uploadpdf/multipart"
    print(f"  POST {url}")
    mime_type = "application/pdf"
    with pdf_path.open("rb") as fh:
        files = {"pdf_file": (pdf_path.name, fh, mime_type)}
        data = {"api_key": api_key}
        resp = requests.post(url, files=files, data=data, timeout=300)
    resp.raise_for_status()
    payload = resp.json()
    pdfid = payload.get("pdfid")
    if not payload.get("success") or not pdfid:
        raise RuntimeError(f"PDF upload failed: {payload}")
    return pdfid


def wait_for_pdf(base_url: str, api_key: str, pdfid: str) -> List[str]:
    url = f"{base_url}/digitalsketch/uploadpdfstatus"
    deadline = time.time() + PDF_TIMEOUT_SECONDS
    last_payload: dict = {}
    check_count = 0
    while time.time() < deadline:
        check_count += 1
        print(f"  POST {url} (Check #{check_count})")
        resp = requests.post(url, json={"api_key": api_key, "pdfid": pdfid}, timeout=60)
        resp.raise_for_status()
        last_payload = resp.json()
        status = last_payload.get("status")
        status_text = last_payload.get("status_text") or STATUS_TEXT.get(status, "unknown")
        pagecount = last_payload.get("pagecount")
        imageids = last_payload.get("imageids") or []
        print(f"    status: {status_text}, pages: {pagecount}, imageids: {len(imageids)}")
        if status == 1:
            if not imageids:
                raise RuntimeError(f"PDF processed but no imageids returned: {last_payload}")
            print(f"  Multi-Page PDF Detected: Found {len(imageids)} pages")
            return imageids
        if status in (2, 3, 4):
            raise RuntimeError(f"PDF processing ended with status {status_text}: {last_payload}")
        time.sleep(PDF_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"PDF processing did not complete within {PDF_TIMEOUT_SECONDS}s. Last: {last_payload}"
    )


def get_image_details(base_url: str, api_key: str, imageid: str) -> dict:
    url = f"{base_url}/digitalsketch/{imageid}/imagedetails"
    print(f"  POST {url}")
    resp = requests.post(url, json={"api_key": api_key}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def start_pipeline(base_url: str, api_key: str, imageid: str) -> dict:
    url = f"{base_url}/digitalsketch/pipeline/start"
    print(f"  POST {url}")
    resp = requests.post(
        url,
        json={"api_key": api_key, "imageid": imageid},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def resolve_run_id(base_url: str, api_key: str, imageid: str, start_payload: dict) -> str:
    run_id = start_payload.get("run_id") or start_payload.get("runid")
    if run_id:
        return run_id

    url = f"{base_url}/digitalsketch/pipeline/id"
    deadline = time.time() + RUN_ID_TIMEOUT_SECONDS
    last_payload: dict = {}
    check_count = 0

    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break

        wait_duration = min(RUN_ID_POLL_INTERVAL_SECONDS, remaining)
        check_count += 1
        print(f"  5.5. GET {url} (Check #{check_count})")

        try:
            resp = requests.post(url, json={"api_key": api_key, "imageid": imageid}, timeout=60)
            resp.raise_for_status()
            last_payload = resp.json()

            if not last_payload.get("success"):
                print(f"    error: {last_payload.get('message', 'Unknown error')}")
            else:
                run_id = last_payload.get("run_id") or last_payload.get("runid")
                if run_id:
                    return run_id
                status = last_payload.get("status")
                print(f"    status: {status}, run_id not yet assigned")
        except requests.RequestException as e:
            print(f"    request error: {e}")

        remaining = deadline - time.time()
        if remaining > 0:
            countdown = min(RUN_ID_POLL_INTERVAL_SECONDS, remaining)
            for sec in range(int(countdown), 0, -1):
                print(f"    waiting {sec}s...", end="\r")
                time.sleep(1)
            print("                  ", end="\r")

    raise TimeoutError(
        f"run_id not assigned within {RUN_ID_TIMEOUT_SECONDS}s for image {imageid}. Last: {last_payload}"
    )


def wait_for_pipeline(base_url: str, api_key: str, run_id: str) -> dict:
    url = f"{base_url}/digitalsketch/pipeline/status"
    deadline = time.time() + PIPELINE_STATUS_TIMEOUT_SECONDS
    last_payload: dict = {}
    check_count = 0

    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break

        check_count += 1
        print(f"  POST {url} (Check #{check_count})")

        try:
            resp = requests.post(url, json={"api_key": api_key, "run_id": run_id}, timeout=60)
            resp.raise_for_status()
            last_payload = resp.json()

            if not last_payload.get("success"):
                print(f"    error: {last_payload.get('message', 'Unknown error')}")
                time.sleep(PIPELINE_STATUS_POLL_INTERVAL_SECONDS)
                continue

            status = last_payload.get("status")
            status_text = last_payload.get("status_text") or STATUS_TEXT.get(status, "unknown")
            completion = last_payload.get("completion")
            progress = f" ({completion})" if completion else ""
            print(f"    status: {status_text}{progress}")

            if status == 1:
                print("    pipeline complete!")
                return last_payload

            if status in (2, 3, 4):
                error_msg = last_payload.get("message") or last_payload.get("error") or "Unknown error"
                print(f"    error message: {error_msg}")
                raise RuntimeError(f"Pipeline ended with status {status_text}. Error: {error_msg}. Full response: {last_payload}")

        except requests.RequestException as e:
            print(f"    request error: {e}")

        remaining = deadline - time.time()
        if remaining > 0:
            countdown = min(PIPELINE_STATUS_POLL_INTERVAL_SECONDS, remaining)
            for sec in range(int(countdown), 0, -1):
                print(f"    waiting {sec}s...", end="\r")
                time.sleep(1)
            print("                  ", end="\r")

    raise TimeoutError(
        f"Pipeline did not complete within {PIPELINE_STATUS_TIMEOUT_SECONDS}s. Last: {last_payload}"
    )


def get_all_info(base_url: str, api_key: str, imageid: str) -> dict:
    url = f"{base_url}/digitalsketch/diagram/info/all"
    print(f"  GET {url}")
    resp = requests.get(
        url,
        params={"api_key": api_key, "imageid": imageid},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def process_image(base_url: str, api_key: str, imageid: str, page_num: int, total_pages: int) -> dict:
    label = f"[Page {page_num}/{total_pages}]"
    print(f"{label} 4. GET /digitalsketch/{{imageid}}/imagedetails")
    details = get_image_details(base_url, api_key, imageid)
    image_size = details.get('imagesize')
    print(f"  image_name={details.get('image_name')} ext={details.get('extension')} size={image_size}")

    if image_size and image_size < 100:
        print(f"  WARNING: Image size is only {image_size} bytes - image may be corrupted!")
        print(f"  Returning empty info for this page.")
        return {
            "success": False,
            "imageid": imageid,
            "info": [],
            "count": 0,
            "error": f"Image size too small ({image_size} bytes), likely corrupted from PDF extraction",
            "timestamp": ""
        }

    print(f"{label} 5. POST /digitalsketch/pipeline/start")
    start_payload = start_pipeline(base_url, api_key, imageid)
    run_id = resolve_run_id(base_url, api_key, imageid, start_payload)
    print(f"  run_id: {run_id}")

    print(f"{label} 6. POST /digitalsketch/pipeline/status")
    wait_for_pipeline(base_url, api_key, run_id)

    print(f"{label} 7. GET /digitalsketch/diagram/info/all")
    return get_all_info(base_url, api_key, imageid)



def main() -> int:
    api_key = os.getenv("DIGITALSKETCH_API_KEY")
    base_url = os.getenv("DIGITALSKETCH_API_BASE", "https://api.digitalsketch.ai").rstrip("/")
    if not api_key:
        print("ERROR: DIGITALSKETCH_API_KEY is not set. Add it to a .env file.", file=sys.stderr)
        return 1

    print(f"Config: PIPELINE_STATUS_POLL_INTERVAL={PIPELINE_STATUS_POLL_INTERVAL_SECONDS}s (timeout {PIPELINE_STATUS_TIMEOUT_SECONDS}s), RUN_ID_POLL_INTERVAL={RUN_ID_POLL_INTERVAL_SECONDS}s (timeout {RUN_ID_TIMEOUT_SECONDS}s)")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = sys.argv[1] if len(sys.argv) > 1 else None
    sample_path = pick_sample_file(filename)
    ext = sample_path.suffix.lower()
    print(f"Using sample file: {sample_path.name}")

    if ext in PDF_EXT:
        print("2. POST /digitalsketch/uploadpdf/multipart")
        pdfid = upload_pdf(base_url, api_key, sample_path)
        print(f"  pdfid: {pdfid}")
        print("3. POST /digitalsketch/uploadpdfstatus")
        imageids = wait_for_pdf(base_url, api_key, pdfid)
    else:
        print("1. POST /digitalsketch/uploadimage")
        imageid = upload_image(base_url, api_key, sample_path)
        print(f"  imageid: {imageid}")
        imageids = [imageid]

    print()
    total_pages = len(imageids)
    is_pdf = ext in PDF_EXT
    print(f"Processing {total_pages} page(s):")
    print()

    for idx, imageid in enumerate(imageids, start=1):
        print("=" * 70)
        print(f"Processing Page {idx} of {total_pages}")
        print("=" * 70)

        info = process_image(base_url, api_key, imageid, idx, total_pages)

        if is_pdf:
            out_path = OUTPUT_DIR / f"{sample_path.stem}_page{idx}_{imageid}_info.json"
        else:
            out_path = OUTPUT_DIR / f"{sample_path.stem}_{imageid}_info.json"

        out_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        print(f"Saved to: {out_path}")
        print()

    print("=" * 70)
    print(f"Completed processing all {total_pages} page(s)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
