#!/usr/bin/env python3
"""
Push the study checklist to a Notion database, and pull your ticks back.

Run make_checklist.py first -- this reads the _checklist_data.json it writes.

The first run creates one database per course under a page you choose -
"Data Structures and Algorithms", "Databases", and so on - each with one row
per file: Name, Type, Done, Path and a "Find in Drive" link. Later runs add
only what's new, and a new course gets its own database the first time it
appears.

Ticks sync both ways. Tick a box in the Notion app on your phone and the
Markdown checklist gets ticked on the next run; tick it in Markdown and Notion
gets updated. Nothing is re-created and nothing is deleted.

    pip install requests
    python notion_sync.py --pages      # list pages the integration can see
    python notion_sync.py --dry-run    # show what would be sent
    python notion_sync.py              # sync

Setup (2 minutes, works on the free plan). Notion now calls these
"connections", not "integrations":
  1. https://app.notion.com/developers/connections
     -> sidebar "Build" -> Internal connections -> Create a new connection.
     Name it, pick your workspace, then open its Configuration tab and copy
     the "Installation access token".
  2. Give it a page: either the connection's "Content access" tab -> Edit
     access, or in Notion open the page -> ... menu -> Connections ->
     + Add connection. A new connection can see nothing until you do this.
  3. python notion_sync.py --pages   to find that page's id.

The first run asks for the token and the page id and offers to save them
into cms_config.json, next to this script. That file is gitignored - delete
it to be asked again. NOTION_TOKEN / NOTION_PARENT_PAGE_ID environment
variables override it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import traceback
from pathlib import Path
from urllib.parse import quote

# --------------------------------------------------------------------------
# CREDENTIALS
# --------------------------------------------------------------------------
NOTION_TOKEN = ""        # ntn_... (or set the NOTION_TOKEN env var)
PARENT_PAGE_ID = ""      # page the database is created under (see --pages)
# Leave both empty: the first run asks for them and offers to save them
# into cms_config.json, the same gitignored file the scraper uses.

# --------------------------------------------------------------------------
# OPTIONS
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_JSON = BASE_DIR / "_checklist_data.json"
STATE_JSON = BASE_DIR / "_notion_state.json"
CONFIG_FILE = BASE_DIR / "cms_config.json"
CHECKLIST_MD = BASE_DIR / "Study Checklist.md"

# Each course gets its own database, named with this. "{course} ({code})"
# works too if you want the code in the title.
DB_TITLE_FORMAT = "{course}"
API = "https://api.notion.com/v1"
# Pinned on purpose: this is the shape the code below speaks. Notion keeps
# older versions working, so don't bump it without checking the payloads.
NOTION_VERSION = "2022-06-28"
TIMEOUT = 30

# Uploading the actual PDFs into Notion needs a paid plan (free caps uploads
# at 5 MiB per file, which most lecture decks exceed). Not built yet -- the
# rows link back to Drive instead.

try:
    import requests
except ImportError:
    sys.exit("Missing dependency (requests).\nRun:  pip install requests")


def log(msg=""):
    print(msg, flush=True)


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
HEX32_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")


def normalise_page_id(value):
    """
    Accept whatever the user pastes: a bare id, a dashed uuid, or the whole
    page URL copied from the browser or from "Copy link" in the app.
    The id is the 32 hex characters at the end of a Notion URL.
    """
    if not value:
        return ""
    trimmed = value.strip().split("?")[0].split("#")[0]
    found = UUID_RE.findall(trimmed) or HEX32_RE.findall(trimmed)
    return found[-1].replace("-", "").lower() if found else ""


# ------------------------------- API ---------------------------------------

class NotionError(RuntimeError):
    pass


def make_session(token):
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer " + token,
                      "Notion-Version": NOTION_VERSION,
                      "Content-Type": "application/json"})
    return s


def api(session, method, path, payload=None):
    r = session.request(method, API + path, timeout=TIMEOUT,
                        data=json.dumps(payload) if payload else None)
    if r.status_code >= 400:
        try:
            body = r.json()
            message = body.get("message", r.text)
            code = body.get("code", "")
        except ValueError:
            message, code = r.text, ""
        if r.status_code == 401:
            raise NotionError("Notion rejected the token (401). Check "
                              "NOTION_TOKEN.")
        if code == "object_not_found":
            raise NotionError(
                "Notion can't see that page or database (404).\n"
                "  In Notion open it -> ... menu -> Connections -> "
                "+ Add connection, then run again.")
        if "version" in message.lower():
            raise NotionError(
                "Notion didn't accept the API version %s:\n  %s\n"
                "  Set NOTION_VERSION near the top of this file to a version "
                "Notion still supports\n  (see "
                "https://developers.notion.com/reference/versioning)."
                % (NOTION_VERSION, message))
        raise NotionError("Notion API %s: %s" % (r.status_code, message))
    return r.json()


def plain_title(obj):
    """Best-effort title of a page object from /v1/search."""
    props = obj.get("properties") or {}
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title") or []
            if parts:
                return "".join(p.get("plain_text", "") for p in parts)
    title = obj.get("title")
    if isinstance(title, list) and title:
        return "".join(p.get("plain_text", "") for p in title)
    return "(untitled)"


def list_pages(session):
    body = api(session, "POST", "/search",
               {"filter": {"value": "page", "property": "object"},
                "page_size": 50})
    results = body.get("results", [])
    if not results:
        log("The connection can't see any pages yet.\n"
            "In Notion: open the page you want to use -> ... menu ->\n"
            "Connections -> + Add connection. Then run this again.")
        return 0
    log("Pages this connection can see - copy one id into PARENT_PAGE_ID:\n")
    for page in results:
        log("  %s   %s" % (page["id"].replace("-", ""), plain_title(page)))
    return 0


# ------------------------------- database ----------------------------------

def db_properties(types):
    # No Course column: each database *is* one course.
    return {
        "Name": {"title": {}},
        "Type": {"select": {"options": [{"name": t} for t in types]}},
        "Done": {"checkbox": {}},
        "Find in Drive": {"url": {}},
        "Path": {"rich_text": {}},
    }


def create_database(session, parent_page_id, title, types):
    body = api(session, "POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": db_properties(types),
    })
    log("  created the '%s' database" % title)
    return body["id"]


def drive_search_url(item):
    """
    A tappable fallback: opens Google Drive searching for that exact file.
    Not a direct link (that needs the Drive API) but one tap from the file.
    """
    return "https://drive.google.com/drive/search?q=" + quote(
        Path(item["path"]).name)


def page_properties(item):
    return {
        "Name": {"title": [{"type": "text",
                            "text": {"content": item["title"][:2000]}}]},
        "Type": {"select": {"name": item["type"]}},
        "Done": {"checkbox": bool(item["done"])},
        "Find in Drive": {"url": drive_search_url(item)},
        "Path": {"rich_text": [{"type": "text",
                                "text": {"content": item["path"][:2000]}}]},
    }


def fetch_rows(session, database_id):
    """-> {path: {page_id, done}} for everything already in the database."""
    rows, cursor = {}, None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        body = api(session, "POST", "/databases/%s/query" % database_id,
                   payload)
        for page in body.get("results", []):
            props = page.get("properties", {})
            path_parts = (props.get("Path") or {}).get("rich_text") or []
            path = "".join(p.get("plain_text", "") for p in path_parts)
            if not path:
                continue
            rows[path] = {"page_id": page["id"],
                          "done": bool((props.get("Done") or {})
                                       .get("checkbox"))}
        if not body.get("has_more"):
            return rows
        cursor = body.get("next_cursor")


# ------------------------------- syncing -----------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return default


def read_config():
    config = load_json(CONFIG_FILE, {})
    return config if isinstance(config, dict) else {}


def save_notion_credentials(token, parent):
    """Merge into cms_config.json - the CMS login lives in there too."""
    config = read_config()
    config["notion_token"] = token
    if parent:
        config["notion_parent_page_id"] = normalise_page_id(parent) or parent
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
        if os.name == "posix":
            os.chmod(CONFIG_FILE, 0o600)
        log("Saved to %s - delete that file to be asked again."
            % CONFIG_FILE.name)
    except OSError as exc:
        log("! couldn't save the token: %s" % exc)


def load_notion_credentials():
    """Environment wins, then cms_config.json, then ask. -> (token, parent)."""
    config = read_config()
    token = (os.getenv("NOTION_TOKEN") or NOTION_TOKEN
             or config.get("notion_token", ""))
    parent = (os.getenv("NOTION_PARENT_PAGE_ID") or PARENT_PAGE_ID
              or config.get("notion_parent_page_id", ""))
    if token:
        return token, parent

    log("No Notion token yet. Create a connection at")
    log("  https://app.notion.com/developers/connections")
    log("  (Build -> Internal connections -> Configuration tab ->")
    log("   Installation access token), then paste it here.")
    try:
        token = getpass.getpass("Notion token (ntn_...): ").strip()
    except EOFError:
        return "", parent
    if not token:
        return "", parent

    typed = input("Parent page id or URL (blank to decide later): ").strip()
    if typed:
        parent = typed
    answer = input("Save these for next time? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        save_notion_credentials(token, parent)
    return token, parent


def merge_done(local, remote, last):
    """
    Three-way merge of one checkbox. `last` is what both sides agreed on at
    the previous sync, so whichever side moved since then wins. If both moved
    the same file in opposite directions, ticked wins -- you did the work.
    """
    if local == remote:
        return local, False, False
    if last is None:
        value = local or remote
        return value, value != remote, value != local
    if local != last and remote == last:
        return local, True, False           # push local -> Notion
    if remote != last and local == last:
        return remote, False, True          # pull Notion -> local
    return True, not remote, not local


def apply_ticks_to_markdown(done_by_path):
    """Write pulled ticks into Study Checklist.md, then let make_checklist
    regenerate it so the counts in the headings stay honest."""
    if not CHECKLIST_MD.exists():
        return
    import re
    from urllib.parse import unquote
    link_re = re.compile(r"^(\s*[-*]\s*\[)([ xX])(\]\s*\[[^\]]*\]\()([^)]+)\)")
    out = []
    for line in CHECKLIST_MD.read_text(encoding="utf-8").splitlines():
        m = link_re.match(line)
        if m:
            path = unquote(m.group(4))
            if path in done_by_path:
                mark = "x" if done_by_path[path] else " "
                line = m.group(1) + mark + m.group(3) + m.group(4) + ")"
        out.append(line)
    CHECKLIST_MD.write_text("\n".join(out) + "\n", encoding="utf-8")

    try:                      # refresh the x / y counts and pick up new files
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "make_checklist", BASE_DIR / "make_checklist.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = sys.argv
        sys.argv = ["make_checklist.py"]
        try:
            module.main()
        finally:
            sys.argv = argv
    except Exception as exc:
        log("! ticks written, but couldn't refresh the counts: %s" % exc)
        log("  run make_checklist.py to tidy them up.")


def group_by_course(items):
    """-> [((code, course), items)] in the order the courses should appear."""
    grouped = {}
    for item in items:
        grouped.setdefault((item["code"], item["course"]), []).append(item)
    return sorted(grouped.items(), key=lambda kv: kv[0][1].lower())


def resolve_parent(args, saved_parent=""):
    """The page the course databases are created under."""
    raw = args.parent or saved_parent or PARENT_PAGE_ID
    if not raw:
        log("! A course has no database yet and PARENT_PAGE_ID isn't set.\n"
            "  Run:  python notion_sync.py --pages\n"
            "  or paste the page's URL into PARENT_PAGE_ID - either works.")
        return ""
    parent = normalise_page_id(raw)
    if not parent:
        log("! Couldn't find a page id in %r.\n"
            "  Paste either the 32-character id or the page's full URL." % raw)
        return ""
    return parent


def sync_course(session, db_id, course_items, last_done, args):
    """One course's database. -> (counts, pulled ticks, done state)."""
    rows = fetch_rows(session, db_id)
    counts = {"created": 0, "pushed": 0, "pulled": 0, "archived": 0}
    pulled_ticks, done_state = {}, {}

    for item in course_items:
        path = item["path"]
        row = rows.get(path)
        if row is None:
            api(session, "POST", "/pages",
                {"parent": {"database_id": db_id},
                 "properties": page_properties(item)})
            counts["created"] += 1
            done_state[path] = item["done"]
            continue

        value, push, pull = merge_done(bool(item["done"]), row["done"],
                                       last_done.get(path))
        if push:
            api(session, "PATCH", "/pages/" + row["page_id"],
                {"properties": {"Done": {"checkbox": value}}})
            counts["pushed"] += 1
        if pull:
            pulled_ticks[path] = value
            counts["pulled"] += 1
        done_state[path] = value

    if args.prune:
        known = {i["path"] for i in course_items}
        for path, row in rows.items():
            if path not in known:
                api(session, "PATCH", "/pages/" + row["page_id"],
                    {"archived": True})
                counts["archived"] += 1
    return counts, pulled_ticks, done_state


def sync(session, items, args, saved_parent=""):
    state = load_json(STATE_JSON, {})
    databases = dict(state.get("databases", {}))
    last_done = state.get("done", {})

    parent = ""
    totals = {"created": 0, "pushed": 0, "pulled": 0, "archived": 0}
    all_pulled, new_state_done = {}, {}

    # Earlier versions kept every course in one database. Move across without
    # losing anything you had already ticked there.
    old_db = state.get("database_id")
    if old_db and not databases:
        log("Found the single combined database from the previous version.\n"
            "  Creating one database per course instead; the old one is left\n"
            "  untouched, so delete it in Notion once you're happy.")
        try:
            carried = {path: row["done"]
                       for path, row in fetch_rows(session, old_db).items()
                       if row["done"]}
        except NotionError as exc:
            carried = {}
            log("  (couldn't read its ticks: %s)" % exc)
        if carried:
            log("  carrying over %d tick(s) from it." % len(carried))
            for item in items:
                if carried.get(item["path"]) and not item["done"]:
                    item["done"] = True
                    all_pulled[item["path"]] = True
        log("")

    for (code, course), course_items in group_by_course(items):
        db_id = databases.get(code)
        if not db_id:
            parent = parent or resolve_parent(args, saved_parent)
            if not parent:
                return 1
            db_id = create_database(
                session, parent,
                DB_TITLE_FORMAT.format(course=course, code=code),
                sorted({i["type"] for i in course_items}))
            databases[code] = db_id

        counts, pulled, done_state = sync_course(
            session, db_id, course_items, last_done, args)
        for key in totals:
            totals[key] += counts[key]
        all_pulled.update(pulled)
        new_state_done.update(done_state)

        log("* %-34s %2d row(s): %d new, %d tick(s) pushed, %d pulled"
            % (course, len(course_items), counts["created"],
               counts["pushed"], counts["pulled"]))

    STATE_JSON.write_text(json.dumps(
        {"databases": databases, "done": new_state_done}, indent=2),
        encoding="utf-8")

    if all_pulled:
        apply_ticks_to_markdown(all_pulled)

    log("\nSynced %d item(s) across %d database(s): %d new row(s), "
        "%d tick(s) pushed, %d pulled back%s."
        % (len(items), len(databases), totals["created"], totals["pushed"],
           totals["pulled"],
           ", %d archived" % totals["archived"] if totals["archived"] else ""))
    for (code, course), _ in group_by_course(items):
        if code in databases:
            log("  %-34s https://www.notion.so/%s"
                % (course, databases[code].replace("-", "")))
    return 0


# --------------------------------- main -----------------------------------

def main():
    ap = argparse.ArgumentParser(description="Sync the study checklist to "
                                             "Notion.")
    ap.add_argument("--pages", action="store_true",
                    help="list pages the integration can see, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be sent, without calling Notion")
    ap.add_argument("--parent", default="",
                    help="page id (or URL) to create the course databases "
                         "under (overrides PARENT_PAGE_ID)")
    ap.add_argument("--prune", action="store_true",
                    help="archive rows whose file no longer exists")
    args = ap.parse_args()

    data = load_json(DATA_JSON, None)
    if not data or not data.get("items"):
        log("! %s is missing or empty - run make_checklist.py first."
            % DATA_JSON.name)
        return 1
    items = data["items"]

    if args.dry_run:
        state = load_json(STATE_JSON, {})
        databases = state.get("databases", {})
        grouped = group_by_course(items)
        missing = [c for (code, c), _ in grouped if code not in databases]
        log("%d item(s) would be synced into %d course database(s)%s."
            % (len(items), len(grouped),
               " (%d to be created: %s)" % (len(missing), ", ".join(missing))
               if missing else ""))
        for (code, course), rows in grouped:
            log("\n%s [%s] (%d)%s"
                % (course, code, len(rows),
                   "" if code in databases else "   <- new database"))
            for item in rows[:4]:
                log("   [%s] %-44s %-18s %s"
                    % ("x" if item["done"] else " ", item["title"][:44],
                       item["type"], drive_search_url(item)[:48] + "..."))
            if len(rows) > 4:
                log("   ... and %d more" % (len(rows) - 4))
        return 0

    token, saved_parent = load_notion_credentials()
    if not token:
        log("! No Notion token - nothing to sync with.")
        return 1

    session = make_session(token)
    try:
        if args.pages:
            return list_pages(session)
        return sync(session, items, args, saved_parent)
    except NotionError as exc:
        log("! %s" % exc)
        return 1
    except requests.RequestException as exc:
        log("! couldn't reach Notion: %s" % exc)
        return 1


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
