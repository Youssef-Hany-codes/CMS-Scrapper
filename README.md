# CMS-Scrapper

Three scripts that keep your GIU coursework in order.

`cms_scraper.py` downloads everything new from the [GIU CMS](https://cms.giu-uni.de)
into per-course folders. `make_checklist.py` sorts what landed into lectures,
tutorials, assignments and solutions, and writes a Markdown checklist.
`notion_sync.py` mirrors that checklist into a Notion database — one per course —
and syncs your ticks both ways, so you can check something off on your phone and
have the Markdown agree next time you run it.

Files are named after **the title shown on the website**, not the CMS's
numeric-and-date filename: `Lecture 3.pdf`, not `GIU1234_2024-10-07.pdf`.

Every step skips work it has already done, so running it twice is safe.

## Install

You need [Python 3.7 or newer](https://www.python.org/downloads/). On Windows,
tick **"Add Python to PATH"** in the installer.

Download this repo (green **Code** button → **Download ZIP**, or
`git clone https://github.com/Youssef-Hany-codes/CMS-Scrapper.git`), then open a
terminal in the folder and install the dependencies:

```bash
py -m pip install -r requirements.txt
```

On macOS or Linux, use `python3 -m pip` instead of `py -m pip`.

The scripts read and write the folder they sit in, so put them wherever you want
your course material to live — a Google Drive folder works well.

## Quick start

Double-click **`run_all.bat`**. It downloads, rebuilds the checklist and syncs
with Notion, pausing after each step so you can read what happened. If a step
fails it carries on with what's already on disk.

The first run asks for your CMS login, and the Notion step asks for a token —
see [Notion setup](#notion-setup) below, or just close the window if you only
want the files.

## The scripts

### `cms_scraper.py` — download

Walks every course you're registered in and downloads each posted file into its
course folder. Already-downloaded files are recorded in `_cms_manifest.json` and
skipped.

| Flag | What it does |
| --- | --- |
| `--list` | List the courses it can see, then exit |
| `--dry-run` | Print what would be downloaded without writing files |
| `--course CODE` | Only this course code; repeat for several |
| `--all` | Also grab courses that aren't in `COURSE_FOLDERS` |
| `--dump-html` | Save each page's HTML next to the script, for debugging |

### `make_checklist.py` — organise

Sorts every downloaded file into a material type — Lectures, Tutorials & Sheets,
Exercises, Labs, Solutions, Exams & Quizzes, Supplementary, Recordings, Course
Info — and writes `Study Checklist.md`. Re-run it after every scrape: new files
are added and anything you already ticked stays ticked.

It uses the type the CMS itself gives each item (`"(Lecture slides)"` in the
filename) and falls back to keyword rules when that label is missing or is the
CMS's meaningless "Other". A file whose name says *solution* is filed as one
whatever the CMS claimed.

When the CMS posts the same handout twice under different ids, both copies are
downloaded but the duplicate is hashed, hidden from the checklist and counted at
the bottom of the course, so you don't study the same PDF twice.

| Flag | What it does |
| --- | --- |
| `--dry-run` | Print the checklist instead of writing it |
| `--stats` | Just the per-course, per-type counts |
| `--out PATH` | Write the checklist somewhere else |

### `notion_sync.py` — sync

Creates one Notion database per course, each row carrying Name, Type, Done, Path
and a "Find in Drive" link. Later runs add only what's new, and a new course gets
its own database the first time it appears. Nothing is deleted.

Ticks merge in both directions. If a box changed on only one side since the last
sync, that side wins; if both changed, ticked wins, because you did the work.

| Flag | What it does |
| --- | --- |
| `--pages` | List pages the connection can see, then exit |
| `--dry-run` | Show what would be sent, without calling Notion |
| `--parent ID` | Page to create the databases under, as an id or a URL |
| `--prune` | Archive rows whose file no longer exists |

## Notion setup

Two minutes, and it works on the free plan. Notion calls these **connections**
now, not integrations.

1. Go to [app.notion.com/developers/connections](https://app.notion.com/developers/connections)
   → sidebar **Build** → **Internal connections** → **Create a new connection**.
   Name it, pick your workspace, then open its **Configuration** tab and copy the
   **Installation access token**.
2. Give it a page. Either the connection's **Content access** tab → **Edit
   access**, or in Notion open the page → **...** menu → **Connections** →
   **+ Add connection**. *A new connection can see nothing until you do this* —
   this is the step everyone misses.
3. Run `python notion_sync.py --pages` to find that page's id.

The first run asks for both and offers to save them.

Uploading the PDFs themselves into Notion isn't supported: the free plan caps
uploads at 5 MiB per file, which most lecture decks exceed. The rows link back to
Drive instead.

## Credentials

Nothing is stored in the scripts. On first run each one asks for what it needs
and offers to save it to `cms_config.json`, which is gitignored and never leaves
your machine. Delete that file to be asked again.

Order of precedence, first match wins:

1. Environment variables — `CMS_USERNAME`, `CMS_PASSWORD`, `CMS_DOMAIN`,
   `NOTION_TOKEN`, `NOTION_PARENT_PAGE_ID`
2. `cms_config.json`
3. An interactive prompt

If the CMS login fails with a 401, it's usually the NTLM domain rather than the
password — delete `cms_config.json` and try `GIU`, `giu-uni.de`, or none.

## Where files go

Courses are matched **strictly by course code** as it appears on the CMS; a
course whose title carries none of the listed codes is skipped unless you pass
`--all`. Edit `COURSE_FOLDERS` near the top of `cms_scraper.py` to map your own —
`make_checklist.py` reads the same mapping, so you only change it in one place:

```python
COURSE_FOLDERS = {
    "INCS104": "Data Structures and Algorithms",
    "INCS103": "Databases",
}
```

Folders are created next to the scripts. Set `PREFIX_WEEK = True` if you'd rather
have `Week 03 - Lecture 3.pdf`.

## What gets generated

All of these are gitignored — they're yours, and every one of them is rebuilt
from the files on disk if you delete it.

| File | What it holds |
| --- | --- |
| `Study Checklist.md` | The checklist you tick |
| `cms_config.json` | Your CMS login and Notion token |
| `_cms_manifest.json` | What has already been downloaded |
| `_checklist_data.json` | The checklist in machine-readable form, for the sync |
| `_notion_state.json` | Database ids and the last agreed tick state |

## Planned

**A GUI.** A small window instead of the terminal: tick the courses you want,
press Download, watch a progress bar, and see your saved login rather than
typing it again. Everything the flags do now, without anyone needing to know
what a flag is. Built with CustomTkinter — flat, rounded, dark-mode-aware
widgets on top of the Tkinter that already ships with Python, so it stays one
small pip install rather than a heavyweight toolkit. Not implemented yet.

## License

MIT — see [LICENSE](LICENSE).
