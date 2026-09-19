#!/usr/bin/env python3
"""
Turn the downloaded course files into a study checklist.

Walks the course folders that cms_scraper.py fills, sorts every file into a
material type (Lectures, Tutorials, Assignments, ...) and writes a Markdown
checklist you can tick off. Re-run it after every scrape: new files are added,
and anything you already ticked stays ticked.

It also writes _checklist_data.json, which the Notion sync reads.

    python make_checklist.py                # update the checklist
    python make_checklist.py --dry-run      # print it, write nothing
    python make_checklist.py --stats        # just the per-course counts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import quote, unquote

BASE_DIR = Path(__file__).resolve().parent
OUT_MD = BASE_DIR / "Study Checklist.md"
OUT_JSON = BASE_DIR / "_checklist_data.json"

# Course code -> folder name. Taken from cms_scraper.py so there is one source
# of truth; the copy below is only used if that file can't be imported.
FALLBACK_COURSE_FOLDERS = {
    "INCS104": "Data Structures and Algorithms",
    "INCS103": "Databases",
    "INCS102": "Operating Systems",
    "MATH304": "Maths 3",
    "INCS101": "Programming 3",
}

# --------------------------------------------------------------------------
# The CMS states each item's type, and cms_scraper.py keeps it in the file
# name: "3 - L2-EERD (Lecture slides).pdf". That label is the best signal we
# have, so it is checked first. Substring match, first hit wins.
# "Other" is CMS's catch-all and means nothing, so it falls through to the
# keyword rules below.
# --------------------------------------------------------------------------
CMS_TYPE_MAP = [
    ("assignment solution", "Solutions"),
    ("solution", "Solutions"),
    ("lecture", "Lectures"),            # "Lecture slides", "Lecture notes"
    ("tutorial", "Tutorials & Sheets"),
    ("worksheet", "Tutorials & Sheets"),
    ("sheet", "Tutorials & Sheets"),
    # Professors label the same thing "Exercise" in one course and
    # "Assignment" in another, so both land in one section.
    ("exercise", "Exercises"),
    ("assignment", "Exercises"),
    ("project", "Exercises"),
    ("lab", "Labs"),
    ("midterm", "Exams & Quizzes"),
    ("exam", "Exams & Quizzes"),
    ("quiz", "Exams & Quizzes"),
    ("supplementary", "Supplementary"),
    ("reference", "Supplementary"),
    ("recording", "Recordings"),
    ("video", "Recordings"),
    ("syllabus", "Course Info"),
    ("course info", "Course Info"),
]

TYPE_PAREN_RE = re.compile(r"\s*\(([^()]{0,40})\)\s*$")
COPY_MARKER_RE = re.compile(r"\s*\((\d{1,3})\)\s*$")   # "... (2)" from a clash

# --------------------------------------------------------------------------
# Fallback for files whose CMS label is missing or "Other". First rule that
# matches the name wins, so the order here is the priority order.
# --------------------------------------------------------------------------
CATEGORY_RULES = [
    ("Solutions", [r"solution", r"\bsol\b", r"\bsols\b", r"\banswers?\b",
                   r"model\s*answer", r"\bkey\b"]),
    ("Exams & Quizzes", [r"\bexam", r"midterm", r"\bmid\b", r"\bfinal\b",
                         r"\bquiz", r"past\s*paper", r"\bmock\b"]),
    ("Exercises", [r"assign", r"exercise", r"homework", r"\bhw\d*\b",
                   r"project", r"milestone", r"\bdeliverable"]),
    ("Labs", [r"\blabs?\b", r"\blab\d", r"practical"]),
    ("Tutorials & Sheets", [r"tutorial", r"\btuts?\b", r"\bsheet",
                            r"practice", r"worksheet", r"recitation",
                            r"\bproblem\s*set"]),
    ("Lectures", [r"lecture", r"\blec\b", r"\blec\d", r"slide", r"chapter",
                  r"\bch\d", r"\bweek\b", r"handout", r"\bnotes?\b"]),
    ("Recordings", [r"record", r"\bvideo\b", r"session\s*\d+\s*video"]),
    ("Course Info", [r"syllabus", r"outline", r"grading", r"policy",
                     r"announce", r"schedule", r"course\s*info",
                     r"\bcontent\b", r"\bplan\b"]),
]

# The order sections appear under each course.
TYPE_ORDER = ["Lectures", "Tutorials & Sheets", "Exercises",
              "Labs", "Solutions", "Exams & Quizzes", "Supplementary",
              "Recordings", "Course Info", "Other Material"]

# A file whose name says "solution" is a solution even when the CMS filed it
# as something else -- e.g. "Worksheet 1 sol. (Tutorial notes)".
SOLUTION_PATTERNS = [r"solution", r"\bsols?\b", r"\banswers?\b",
                     r"model\s*answer"]

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg"}

# Files that are never study material.
IGNORE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "_cms_manifest.json",
                "_checklist_data.json", "cms_scraper.py", "make_checklist.py",
                "notion_sync.py", "study checklist.md"}
IGNORE_SUFFIXES = {".part", ".tmp", ".crdownload", ".lnk"}
IGNORE_PREFIXES = ("_dump_", "~$", ".")


# ------------------------------- helpers ----------------------------------

def log(msg=""):
    print(msg, flush=True)


def load_course_folders():
    """Prefer the mapping in cms_scraper.py so the two scripts can't drift."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cms_scraper", BASE_DIR / "cms_scraper.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        folders = getattr(module, "COURSE_FOLDERS", None)
        if isinstance(folders, dict) and folders:
            return folders
    except Exception:
        pass
    log("! using the built-in course list (cms_scraper.py wasn't importable)")
    return FALLBACK_COURSE_FOLDERS


def is_ignored(path):
    name = path.name.lower()
    if name in IGNORE_NAMES or path.suffix.lower() in IGNORE_SUFFIXES:
        return True
    return name.startswith(IGNORE_PREFIXES)


def map_cms_label(label):
    """'Lecture slides' -> 'Lectures'. None if the label tells us nothing."""
    for needle, category in CMS_TYPE_MAP:
        if needle in label:
            return category
    return None


def analyse(path):
    """-> (display title, cms label, material type) for one file."""
    stem = path.stem

    # A " (2)" the downloader added to avoid overwriting a same-named file
    # sits after the CMS label, so lift it off first and put it back at the
    # end -- otherwise it hides the label from the check below.
    copy_marker = ""
    m_copy = COPY_MARKER_RE.search(stem)
    if m_copy:
        copy_marker = " (%s)" % m_copy.group(1)
        stem = stem[:m_copy.start()]

    m = TYPE_PAREN_RE.search(stem)
    label = " ".join(m.group(1).split()).lower() if m else ""
    mapped = map_cms_label(label) if label else None

    # Drop the trailing "(Lecture slides)" from the title -- the section
    # heading already says it. Keep unrecognised parentheses, they're part
    # of the real name, e.g. "Entity-Relationship Model (ERD)".
    if m and (mapped or label == "other"):
        display = (stem[:m.start()].strip() or stem) + copy_marker
    else:
        display = stem + copy_marker

    haystack = display.lower()
    suffix = path.suffix.lower()

    if suffix in VIDEO_EXT or suffix in AUDIO_EXT:
        return display, label, "Recordings"
    for pattern in SOLUTION_PATTERNS:
        if re.search(pattern, haystack):
            return display, label, "Solutions"
    if mapped:
        return display, label, mapped

    folder_hint = path.parent.name.lower()   # a file in "Assignments/" follows it
    for name, patterns in CATEGORY_RULES:
        for pattern in patterns:
            if re.search(pattern, haystack) or re.search(pattern, folder_hint):
                return display, label, name
    return display, label, "Other Material"


def natural_key(text):
    """Sort '2 - Lecture 2' before '10 - Lecture 10'."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)]


def rel_posix(path):
    return path.relative_to(BASE_DIR).as_posix()


LINK_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*\[[^\]]*\]\(([^)]+)\)")


def read_existing_ticks(md_path):
    """Pull '- [x] ... (path)' lines out of the current checklist."""
    ticks = {}
    if not md_path.exists():
        return ticks
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ticks
    for line in text.splitlines():
        m = LINK_RE.match(line)
        if m:
            ticks[unquote(m.group(2))] = m.group(1).lower() == "x"
    return ticks


# ------------------------------- gathering --------------------------------

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def mark_duplicates(records):
    """
    The CMS often carries the same handout twice under different ids, so the
    scraper legitimately downloads both. Keep the first copy in the checklist
    and mark the rest, so you don't study the same PDF twice.
    """
    by_size = {}
    for record in records:
        by_size.setdefault(record["size"], []).append(record)
    for group in by_size.values():
        if len(group) < 2:
            continue
        seen = {}
        for record in group:                      # already in natural order
            try:
                digest = file_hash(record["path"])
            except OSError:
                continue
            if digest in seen:
                record["duplicate_of"] = rel_posix(seen[digest]["path"])
            else:
                seen[digest] = record


def collect(course_folders):
    """-> [{code, course, folder, records, types: {type: [records]}, count}]"""
    courses = []
    for code, folder_name in course_folders.items():
        folder = BASE_DIR / folder_name
        if not folder.is_dir():
            continue
        files = [p for p in folder.rglob("*")
                 if p.is_file() and not is_ignored(p)]

        records = []
        for path in sorted(files, key=lambda p: natural_key(p.name)):
            display, label, type_name = analyse(path)
            records.append({"path": path, "display": display,
                            "cms_type": label, "type": type_name,
                            "size": path.stat().st_size,
                            "duplicate_of": None})
        mark_duplicates(records)

        types = {}
        for record in records:
            if not record["duplicate_of"]:
                types.setdefault(record["type"], []).append(record)

        courses.append({"code": code, "course": folder_name, "folder": folder,
                        "records": records, "types": types,
                        "count": len(files),
                        "dupes": sum(1 for r in records if r["duplicate_of"])})
    courses.sort(key=lambda c: c["course"].lower())
    return courses


def build_items(courses, ticks):
    """Flatten into checklist rows, carrying over the ticks we already had."""
    items = []
    for course in courses:
        for type_name in sorted(course["types"],
                                key=lambda t: (TYPE_ORDER.index(t)
                                               if t in TYPE_ORDER else 99, t)):
            for record in course["types"][type_name]:
                rel = rel_posix(record["path"])
                items.append({
                    "code": course["code"],
                    "course": course["course"],
                    "type": type_name,
                    "title": record["display"],
                    "cms_type": record["cms_type"],
                    "path": rel,
                    "ext": record["path"].suffix.lower().lstrip("."),
                    "size": record["size"],
                    "done": ticks.get(rel, False),
                })
    return items


# ------------------------------- rendering --------------------------------

def render(courses, items):
    by_key = {}
    for item in items:
        by_key.setdefault((item["course"], item["type"]), []).append(item)

    done_total = sum(1 for i in items if i["done"])
    lines = ["# Study Checklist", ""]
    lines.append("Re-run `make_checklist.py` after each scrape - new files are "
                 "added and your ticks are kept.")
    lines.append("")
    lines.append("**Overall: %d / %d done**" % (done_total, len(items)))
    lines.append("")

    for course in courses:
        course_items = [i for i in items if i["course"] == course["course"]]
        if not course_items:
            continue
        done = sum(1 for i in course_items if i["done"])
        lines.append("## %s  (%s) - %d / %d"
                     % (course["course"], course["code"], done,
                        len(course_items)))
        lines.append("")
        for type_name in TYPE_ORDER + sorted(
                t for t in course["types"] if t not in TYPE_ORDER):
            rows = by_key.get((course["course"], type_name))
            if not rows:
                continue
            lines.append("### %s (%d)" % (type_name, len(rows)))
            for item in rows:
                lines.append("- [%s] [%s](%s)"
                             % ("x" if item["done"] else " ",
                                item["title"].replace("]", ")"),
                                quote(item["path"])))
            lines.append("")
        if course["dupes"]:
            lines.append("_%d duplicate upload(s) hidden - the CMS posted the "
                         "same file twice._" % course["dupes"])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------- main -----------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build a study checklist from the "
                                             "downloaded course files.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the checklist instead of writing it")
    ap.add_argument("--stats", action="store_true",
                    help="only show how many files each course/type has")
    ap.add_argument("--out", default=str(OUT_MD), help="checklist path")
    args = ap.parse_args()

    out_md = Path(args.out)
    if not out_md.is_absolute():
        out_md = BASE_DIR / out_md

    course_folders = load_course_folders()
    courses = collect(course_folders)
    if not courses:
        log("! none of the course folders exist yet - run cms_scraper.py first.")
        return 1

    ticks = read_existing_ticks(out_md)
    items = build_items(courses, ticks)

    if not items:
        log("! the course folders are empty - run cms_scraper.py first.")
        return 1

    if args.stats:
        for course in courses:
            log("%s (%s) - %d file(s)"
                % (course["course"], course["code"], course["count"]))
            for type_name in TYPE_ORDER + sorted(
                    t for t in course["types"] if t not in TYPE_ORDER):
                rows = course["types"].get(type_name)
                if rows:
                    log("    %-24s %d" % (type_name, len(rows)))
            if course["dupes"]:
                log("    %-24s %d (not listed)" % ("duplicate uploads",
                                                   course["dupes"]))
        return 0

    text = render(courses, items)

    if args.dry_run:
        log(text)
        return 0

    out_md.write_text(text, encoding="utf-8")
    OUT_JSON.write_text(json.dumps(
        {"items": items, "generated_from": str(BASE_DIR)},
        indent=2, ensure_ascii=False), encoding="utf-8")

    kept = sum(1 for i in items if i["done"])
    log("Wrote %s" % out_md.name)
    log("  %d file(s) across %d course(s), %d already ticked."
        % (len(items), len(courses), kept))
    for course in courses:
        n = sum(1 for i in items if i["course"] == course["course"])
        log("    %-34s %3d" % (course["course"], n))
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        log("\nInterrupted.")
        code = 130
    except Exception:
        traceback.print_exc()
    try:
        input("\nPress Enter to exit...")
    except EOFError:
        pass
    sys.exit(code)
