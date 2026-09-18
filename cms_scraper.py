#!/usr/bin/env python3
"""
GIU CMS course-material scraper.

Logs into https://cms.giu-uni.de (NTLM), walks every course you are registered
in, and downloads each posted file into the matching local course folder using
the *title shown on the website* instead of the CMS's numeric+date filename.

Running it twice will not re-download anything: every file is recorded in
_cms_manifest.json next to this script, and existing files are skipped.

    pip install requests requests_ntlm beautifulsoup4
    python cms_scraper.py                 # download everything new
    python cms_scraper.py --list          # just show the courses it can see
    python cms_scraper.py --dry-run       # show what would be downloaded
    python cms_scraper.py --course INCS104
"""

from __future__ import annotations

import argparse
import getpass
import html
import json
import os
import re
import sys
import traceback
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

# --------------------------------------------------------------------------
# 1. CREDENTIALS  -- put yours here (or set CMS_USERNAME / CMS_PASSWORD env vars)
# --------------------------------------------------------------------------
USERNAME = ""          # e.g. "yousef.samy"  (your CMS/university login)
PASSWORD = ""          # your CMS password
DOMAIN = "giu-uni.de"         # NTLM domain. If login fails, try "" or "giu-uni.de"

# --------------------------------------------------------------------------
# 2. WHERE FILES GO  -- course code  ->  folder name (relative to this script)
#    Matching is STRICTLY by these course codes as they appear on the CMS.
#    A course whose title carries none of them is skipped (unless --all).
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

COURSE_FOLDERS = {
    "INCS104": "Data Structures and Algorithms",
    "INCS103": "Databases",
    "INCS102": "Operating Systems",
    "MATH304": "Maths 3",
    "INCS101": "Programming 3",
}

# --------------------------------------------------------------------------
# 3. OPTIONS
# --------------------------------------------------------------------------
BASE_URL = "https://cms.giu-uni.de"
HOME_URL = BASE_URL + "/apps/student/HomePageStn.aspx"
MANIFEST = BASE_DIR / "_cms_manifest.json"
CONFIG_FILE = BASE_DIR / "cms_config.json"
PREFIX_WEEK = False     # True -> "Week 03 - Lecture 3.pdf"
SKIP_UNMAPPED = True    # True -> ignore courses whose code isn't listed above
TIMEOUT = 60
VERIFY_SSL = True       # set False only if your network breaks TLS

FILE_EXT_RE = re.compile(
    r"\.(pdf|pptx?|docx?|xlsx?|zip|rar|7z|txt|csv|java|py|c|cpp|h|ipynb|"
    r"mp4|mkv|avi|mp3|m4a|wav|png|jpe?g|gif|svg|tex|jar|exe|msi)$", re.I)

# --------------------------------------------------------------------------

try:
    import requests
    from requests_ntlm import HttpNtlmAuth
    from bs4 import BeautifulSoup
except ImportError as exc:                                    # pragma: no cover
    sys.exit("Missing dependency (%s).\n"
             "Run:  pip install requests requests_ntlm beautifulsoup4"
             % getattr(exc, "name", exc))


# ------------------------------- helpers ----------------------------------

def log(msg=""):
    print(msg, flush=True)


def clean_text(value):
    """Collapse whitespace / unescape entities in a chunk of page text."""
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def safe_filename(name, max_len=120):
    """Turn a page title into something Windows will accept as a file name."""
    name = unicodedata.normalize("NFKC", clean_text(name))
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "untitled"


CODE_RE = re.compile(r"\b([A-Za-z]{2,6})[\s\-_]*([0-9]{3})\b")


STRIP_LEADING_INDEX = True
LEADING_INDEX_RE = re.compile(r"^\s*\d{1,3}\s*[-.)]\s*")


def strip_leading_index(name):
    """
    '3 - Lecture 3 Stacks' -> 'Lecture 3 Stacks'.

    The CMS prefixes every item with its position on the course page, which
    is noise once the file is on disk. Set STRIP_LEADING_INDEX = False above
    to keep it.
    """
    if not STRIP_LEADING_INDEX:
        return name
    stripped = LEADING_INDEX_RE.sub("", name, count=1).strip()
    return stripped or name          # never strip a name down to nothing


def course_codes(title):
    """Every code-shaped token in a title: '(|INCS 104|) Data ...' -> ['INCS104']."""
    return [(m.group(1) + m.group(2)).upper() for m in CODE_RE.finditer(title)]


def folder_for(title):
    """
    Map a CMS course title to (folder name, manifest key), or None to skip it.

    Strictly code-based: the title must contain one of the COURSE_FOLDERS
    codes. Course names/keywords are never used to guess a folder.
    """
    codes = course_codes(title)
    for code in codes:
        if code in COURSE_FOLDERS:
            return COURSE_FOLDERS[code], code
    if SKIP_UNMAPPED:
        return None
    # --all: park anything unrecognised under a folder named after the course.
    return safe_filename(title)[:60], (codes[0] if codes else safe_filename(title))


def load_manifest():
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log("! manifest unreadable, starting a fresh one")
    return {}


def save_manifest(data):
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")


def save_credentials(user, pwd, domain):
    try:
        CONFIG_FILE.write_text(json.dumps(
            {"username": user, "password": pwd, "domain": domain}, indent=2),
            encoding="utf-8")
        if os.name == "posix":
            os.chmod(CONFIG_FILE, 0o600)
        log("Saved to %s - delete that file to be asked again."
            % CONFIG_FILE.name)
    except OSError as exc:
        log("! couldn't save credentials: %s" % exc)


def load_credentials():
    """Environment wins, then cms_config.json, then ask."""
    config = {}
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log("! %s is unreadable - asking again." % CONFIG_FILE.name)

    user = os.getenv("CMS_USERNAME") or USERNAME or config.get("username", "")
    pwd = os.getenv("CMS_PASSWORD") or PASSWORD or config.get("password", "")
    domain = os.getenv("CMS_DOMAIN") or config.get("domain") or DOMAIN

    asked = False
    if not user:
        user = input("CMS username: ").strip()
        asked = True
    if not pwd:
        pwd = getpass.getpass("CMS password: ")
        asked = True

    if asked:
        typed = input("NTLM domain [%s]: " % (domain or "none")).strip()
        if typed:
            domain = "" if typed.lower() in ("none", "-") else typed
        answer = input("Save these for next time? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            save_credentials(user, pwd, domain)

    return user, pwd, domain


# ------------------------------- session ----------------------------------

def make_session(user, password, domain):
    account = (domain + "\\" + user) if domain else user
    s = requests.Session()
    s.auth = HttpNtlmAuth(account, password)
    s.verify = VERIFY_SSL
    s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0 Safari/537.36")
    return s


def fetch(session, url):
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r


# ------------------------------- parsing ----------------------------------

def find_courses(soup):
    """Every course link on the CMS home page."""
    courses, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"(CourseViewStn|ViewAllCourseStn)", href, re.I):
            continue
        url = urljoin(BASE_URL + "/apps/student/", href)
        qs = parse_qs(urlparse(url).query)
        cid = (qs.get("id") or qs.get("courseId") or [""])[0]
        sid = (qs.get("sid") or [""])[0]
        key = (cid, sid)
        if not cid or key in seen:
            continue
        seen.add(key)

        title = clean_text(a.get_text())
        row = a.find_parent("tr")
        if row:                                  # the row holds code + name
            cells = [clean_text(td.get_text()) for td in row.find_all("td")]
            joined = " ".join(c for c in cells if c and len(c) < 120)
            if joined:
                title = joined
        if not title:
            title = "course " + cid
        courses.append({"id": cid, "sid": sid, "title": title, "url": url})
    return courses


def is_file_link(href):
    return bool(re.search(r"download\.aspx|/upload/|getfile|filehandler",
                          href, re.I)
                or FILE_EXT_RE.search(href.split("?")[0]))


def entry_title(anchor):
    """
    The name shown on the site for one content card.

    CMS cards look roughly like:
        <div class="card mb-5">
          <div class="card-body">
            <strong>Lecture</strong>
            <div>3 - Lecture 3 Stacks and Queues</div>
            <a id="download" href="/apps/student/Download.aspx?id=...">Download</a>
    so we climb to the card and pick the most descriptive line inside it.
    """
    node, card = anchor, None
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        classes = " ".join(node.get("class", [])) if hasattr(node, "get") else ""
        if "card" in classes or node.name in ("li", "tr"):
            card = node
            if "card" in classes:
                break
    card = card or anchor.parent

    candidates = []
    if card is not None:
        for tag in card.find_all(["div", "span", "strong", "b", "h1", "h2",
                                  "h3", "h4", "p", "td"]):
            if tag.find(["div", "table"]):        # keep only leaf-ish text
                continue
            text = clean_text(tag.get_text())
            if text and len(text) < 200:
                candidates.append(text)
        for inp in card.find_all("input"):
            val = clean_text(inp.get("value", ""))
            if val and len(val) < 200:
                candidates.append(val)

    noise = re.compile(r"^(download|view|open|content|description|week|"
                       r"n/?a|-)?$", re.I)
    numbered = [c for c in candidates if re.match(r"^\d+\s*[-.)]\s*\S", c)]
    if numbered:
        return max(numbered, key=len)

    useful = [c for c in candidates
              if not noise.match(c) and not c.lower().startswith("download")
              and len(c) > 3]
    if useful:
        return max(useful, key=len)

    own = clean_text(anchor.get_text()) or clean_text(anchor.get("title", ""))
    if own and not noise.match(own):
        return own
    return ""


def week_label(anchor):
    node = anchor
    for _ in range(10):
        node = node.parent
        if node is None:
            return ""
        header = node.find(["h1", "h2", "h3", "h4"]) if hasattr(node, "find") else None
        if header:
            text = clean_text(header.get_text())
            m = re.search(r"week[:\s]*([\w\- /]+)", text, re.I)
            if m:
                return safe_filename("Week " + m.group(1))[:40]
    return ""


def find_entries(soup, page_url):
    entries, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if not is_file_link(href):
            continue
        url = urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)
        qs = parse_qs(urlparse(url).query)
        fid = (qs.get("id") or qs.get("fileId") or [""])[0] or url
        entries.append({"url": url, "fid": str(fid),
                        "title": entry_title(a),
                        "week": week_label(a) if PREFIX_WEEK else ""})
    return entries


def filename_from_headers(resp):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, re.I)
    return html.unescape(m.group(1).strip()) if m else ""


def extension_for(resp, url, fallback=".pdf"):
    served = filename_from_headers(resp)
    if served:
        ext = os.path.splitext(served)[1]
        if ext and len(ext) <= 6:
            return ext.lower()
    ext = os.path.splitext(urlparse(url).path)[1]
    if ext and len(ext) <= 6 and FILE_EXT_RE.search(ext):
        return ext.lower()
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/msword": ".doc",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "text/plain": ".txt",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "image/png": ".png",
        "image/jpeg": ".jpg",
    }.get(ctype, fallback)


def unique_path(folder, stem, ext):
    target = folder / (stem + ext)
    n = 2
    while target.exists():
        target = folder / ("%s (%d)%s" % (stem, n, ext))
        n += 1
    return target


# ------------------------------ downloading -------------------------------

def download(session, entry, folder, manifest, key, dry_run):
    """Returns 'skipped', 'downloaded' or raises."""
    record = manifest.get(key)
    if record:
        existing = BASE_DIR / record["path"]
        if existing.exists() and existing.stat().st_size > 0:
            return "skipped"

    stem = (safe_filename(strip_leading_index(entry["title"]))
            if entry["title"] else "")
    if entry["week"] and stem:
        stem = entry["week"] + " - " + stem

    if dry_run:
        log("    would download: %s  <- %s"
            % (stem or "(name from server)", entry["url"]))
        return "downloaded"

    with session.get(entry["url"], stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        ext = extension_for(r, entry["url"])

        if not stem:                       # no title on the page -> server name
            served = filename_from_headers(r)
            stem = (safe_filename(os.path.splitext(served)[0]) if served
                    else "file " + entry["fid"])

        already = folder / (stem + ext)
        if already.exists() and already.stat().st_size > 0:
            # Same title already on disk from an earlier run -> nothing to do.
            manifest[key] = {"path": str(already.relative_to(BASE_DIR)),
                             "url": entry["url"], "title": entry["title"]}
            return "skipped"

        folder.mkdir(parents=True, exist_ok=True)
        target = unique_path(folder, stem, ext)
        part = target.with_name(target.name + ".part")
        size = 0
        with open(part, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
                    size += len(chunk)
        if size == 0:
            part.unlink()
            raise IOError("empty response")
        part.replace(target)

    manifest[key] = {"path": str(target.relative_to(BASE_DIR)),
                     "url": entry["url"], "title": entry["title"], "size": size}
    log("    + %s  (%.0f KB)" % (target.name, size / 1024.0))
    return "downloaded"


# --------------------------------- main -----------------------------------

def main():
    ap = argparse.ArgumentParser(description="Download GIU CMS course files.")
    ap.add_argument("--list", action="store_true", help="list courses and exit")
    ap.add_argument("--dry-run", action="store_true", help="don't write files")
    ap.add_argument("--course", action="append", default=[],
                    help="only this course code, e.g. INCS104 (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="also grab courses that aren't in COURSE_FOLDERS")
    ap.add_argument("--dump-html", action="store_true",
                    help="save each page's HTML next to the script (debugging)")
    args = ap.parse_args()

    global SKIP_UNMAPPED
    if args.all:
        SKIP_UNMAPPED = False

    user, pwd, domain = load_credentials()

    session = make_session(user, pwd, domain)

    log("Signing in to %s as %s ..." % (BASE_URL, user))
    try:
        home = fetch(session, HOME_URL)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            log("! 401 Unauthorized - wrong username/password, or the wrong "
                "NTLM domain.\n  Delete %s and run again to re-enter them."
                % CONFIG_FILE.name)
            return 1
        raise
    except requests.RequestException as e:
        log("! could not reach the CMS: %s" % e)
        return 1

    if args.dump_html:
        (BASE_DIR / "_dump_home.html").write_text(home.text, encoding="utf-8")

    courses = find_courses(BeautifulSoup(home.text, "html.parser"))
    if not courses:
        log("! no course links found on the home page. Run with --dump-html "
            "and check _dump_home.html.")
        return 1

    log("Found %d course(s) on the CMS.\n" % len(courses))
    if args.list:
        for c in courses:
            dest = folder_for(c["title"])
            log("  %s\n      -> %s   [%s]"
                % (c["title"], dest[0] if dest else "(not mapped)", c["url"]))
        return 0

    manifest = load_manifest()
    totals = {"downloaded": 0, "skipped": 0, "failed": 0}

    for course in courses:
        dest = folder_for(course["title"])
        if dest is None:
            log("- skipping unmapped course: %s" % course["title"])
            continue
        folder_name, ckey = dest

        if args.course:
            wanted = {f.upper().replace(" ", "") for f in args.course}
            if ckey.upper() not in wanted:
                continue

        log("* %s  ->  %s" % (course["title"], folder_name))
        folder = BASE_DIR / folder_name
        try:
            page = fetch(session, course["url"])
        except requests.RequestException as e:
            log("    ! could not open course page: %s" % e)
            totals["failed"] += 1
            continue

        if args.dump_html:
            (BASE_DIR / ("_dump_%s.html" % ckey)).write_text(page.text,
                                                             encoding="utf-8")

        entries = find_entries(BeautifulSoup(page.text, "html.parser"),
                               course["url"])
        if not entries:
            log("    (no files posted yet)")
            continue

        for entry in entries:
            key = "%s:%s" % (ckey, entry["fid"])
            try:
                totals[download(session, entry, folder, manifest, key,
                                args.dry_run)] += 1
            except Exception as e:                       # keep going on errors
                log("    ! failed %s: %s" % (entry["title"] or entry["url"], e))
                totals["failed"] += 1

        if not args.dry_run:
            save_manifest(manifest)

    log("\nDone. %d new, %d already had, %d failed."
        % (totals["downloaded"], totals["skipped"], totals["failed"]))
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        log("\nInterrupted.")
        code = 130
    except Exception:                  # show the error instead of a vanishing window
        traceback.print_exc()
    try:
        input("\nPress Enter to exit...")
    except EOFError:                   # no console attached (piped / scheduled run)
        pass
    sys.exit(code)
