# LoTW ADIF Downloader
# Author: George Perkins (N5UI)
# Date: 2026-11-19
# Version: 1.2.0
#
# Usage: py download_lotw.py [--date YYYY-MM-DD]
#
# No options          Download all available QSOs to Downloads/LoTW-Full.adi.
# --date YYYY-MM-DD   Download QSOs made on one UTC date to
#                     Downloads/LoTW-YYYY-MM-DD.adi.
# -h, --help          Show command-line help and exit.
#
# Examples:
#   py download_lotw.py
#   py download_lotw.py --date 2026-11-19
#
# The script prompts for your LoTW username and password. The ADIF export
# includes confirmed and unconfirmed QSOs.

"""Download the complete ARRL LoTW QSO history as a raw ADIF file."""

import argparse
from datetime import date
import getpass
import os
from pathlib import Path
from http.client import HTTPException
import re
import sys
import tempfile
from threading import Event, Thread
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener

ENDPOINT = "https://lotw.arrl.org/lotwuser/lotwreport.adi"


class NoRedirects(HTTPRedirectHandler):
    """Keep the credential-bearing request on the documented endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def downloads_folder():
    if os.name == "nt":
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        downloads_id = "{374DE290-123F-4565-9164-39C4925E467B}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, downloads_id)
                return Path(winreg.ExpandEnvironmentStrings(value))
        except OSError:
            pass
    return Path.home() / "Downloads"


def download_report(username, password, qso_date=None):
    parameters = {
        "login": username,
        "password": password,
        "qso_query": "1",
        "qso_qsl": "no",
        "qso_qsorxsince": "1900-01-01",
        "qso_qsldetail": "yes",
        "qso_mydetail": "yes",
        "qso_withown": "yes",
    }
    if qso_date:
        parameters.update(qso_startdate=qso_date, qso_enddate=qso_date)

    query = urlencode(parameters)
    finished = Event()
    started = monotonic()
    received = 0
    headers_received = False

    def show_progress():
        while not finished.wait(5):
            elapsed = int(monotonic() - started)
            if headers_received:
                print(f"  Received {received:,} bytes ({elapsed}s elapsed)...", flush=True)
            else:
                print(f"  Waiting for LoTW to respond ({elapsed}s elapsed)...", flush=True)

    progress = Thread(target=show_progress, daemon=True)
    progress.start()
    try:
        with build_opener(NoRedirects()).open(f"{ENDPOINT}?{query}", timeout=120) as response:
            headers_received = True
            print("LoTW responded. Reading the ADIF report...", flush=True)
            chunks = []
            while chunk := response.read(64 * 1024):
                chunks.append(chunk)
                received += len(chunk)
            if response.length:
                raise ValueError("LoTW closed the connection before sending the complete report.")
            report = b"".join(chunks)
    finally:
        finished.set()
        progress.join()

    return report, validate_report(report)


def validate_report(report):
    header, marker, records = report.upper().partition(b"<EOH>")
    if not marker or not re.search(rb"<PROGRAMID:4(?::[A-Z])?>LOTW", header):
        raise ValueError("LoTW did not return a valid ADIF report. Check your credentials.")
    # LoTW can append an EOF field after the final record, without another EOR.
    records, eof, footer_value = records.partition(b"<APP_LOTW_EOF")
    if eof:
        footer = re.fullmatch(rb"(?::([0-9]+)(?::[A-Z])?)?>(.*)", footer_value, re.DOTALL)
        if not footer or footer.group(2)[int(footer.group(1) or 0):].strip() or (
            len(footer.group(2).rstrip(b"\r\n")) < int(footer.group(1) or 0)
        ):
            raise ValueError("LoTW returned an invalid end-of-file field.")
    if records.strip() and not records.rstrip().endswith(b"<EOR>"):
        raise ValueError("The last LoTW QSO is missing its end-of-record marker.")
    count = records.count(b"<EOR>")
    advertised = re.search(rb"<APP_LOTW_NUMREC:[0-9]+(?::[A-Z])?>\s*([0-9]+)", header)
    if advertised and count != int(advertised.group(1)):
        raise ValueError(f"LoTW reported {int(advertised.group(1)):,} QSOs but sent {count:,} complete records.")
    return count


def save_report(report, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(report)
        temp_path.replace(destination)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Download QSOs on one UTC date.")
    args = parser.parse_args()
    qso_date = None
    if args.date:
        try:
            qso_date = date.fromisoformat(args.date).isoformat()
        except ValueError:
            parser.error("--date must be a valid date in YYYY-MM-DD format.")

    try:
        username = input("LoTW username: ").strip()
        if not sys.stdin.isatty():
            raise ValueError("Run this program in a terminal so the password can be hidden.")
        password = getpass.getpass("LoTW password: ")
        if not username or not password:
            raise ValueError("Enter both your LoTW username and password.")

        filename = f"LoTW-{qso_date}.adi" if qso_date else "LoTW-Full.adi"
        destination = downloads_folder() / filename
        if destination.exists():
            answer = input(f"Replace {destination}? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Canceled.")
                return 1

        scope = f"QSOs on {qso_date} UTC" if qso_date else "your full LoTW history"
        print(f"Requesting {scope} from LoTW...", flush=True)
        report, count = download_report(username, password, qso_date)
        save_report(report, destination)
        print(f"Saved {count:,} QSOs ({len(report):,} bytes) to {destination}")
        if count == 0:
            print("LoTW returned zero matching QSOs.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nCanceled.", file=sys.stderr)
    except HTTPError as error:
        print(f"LoTW returned HTTP {error.code}. Check your credentials or try again later.", file=sys.stderr)
    except (URLError, TimeoutError) as error:
        if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
            print("The connection to LoTW timed out. Please try again.", file=sys.stderr)
        else:
            print("Could not connect to LoTW. Please try again.", file=sys.stderr)
    except HTTPException:
        print("The connection ended before the LoTW download completed.", file=sys.stderr)
    except ValueError as error:
        print(error, file=sys.stderr)
    except OSError:
        print("The download or file write failed. Check your connection and Downloads folder.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
