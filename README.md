# CMS-Scrapper

A Python scraper for the [GIU CMS](https://cms.giu-uni.de). It logs in over NTLM,
walks every course you're registered in, and downloads each posted file into the
matching local course folder — named after **the title shown on the website**
rather than the CMS's numeric-and-date filename.

`Lecture 3.pdf`, not `GIU1234_2024-10-07.pdf`.

Running it twice downloads nothing twice. Every file is recorded in
`_cms_manifest.json` next to the script, and anything already on disk is skipped,
so it's safe to run after every lecture.

## Install

You need [Python 3.7 or newer](https://www.python.org/downloads/). On Windows,
tick **"Add Python to PATH"** in the installer.

Download this repo (green **Code** button → **Download ZIP**, or
`git clone https://github.com/Youssef-Hany-codes/CMS-Scrapper.git`), open a
terminal in the folder, and install the two dependencies:

```bash
py -m pip install -r requirements.txt
```

On macOS or Linux, use `python3 -m pip` instead of `py -m pip`.

## Usage

```bash
python cms_scraper.py                 # download everything new
python cms_scraper.py --list          # just show the courses it can see
python cms_scraper.py --dry-run       # show what would be downloaded
python cms_scraper.py --course INCS104
```

| Flag | What it does |
| --- | --- |
| `--list` | List the courses it can see, then exit |
| `--dry-run` | Print what would be downloaded without writing files |
| `--course CODE` | Only this course code; repeat for several |
| `--all` | Also grab courses that aren't in `COURSE_FOLDERS` |
| `--dump-html` | Save each page's HTML next to the script, for debugging |

## Credentials

On first run it asks for your CMS username, password and NTLM domain, then
offers to save them to `cms_config.json` so it can stop asking. That file never 
leaves your machine — delete it to be asked again.

If login fails with a 401, it's usually the NTLM domain rather than the
password — delete `cms_config.json` and try `GIU`, `giu-uni.de`, or none.

## Where files go

Courses are matched **strictly by course code** as it appears on the CMS; a
course whose title carries none of the listed codes is skipped unless you pass
`--all`. Edit `COURSE_FOLDERS` near the top of the script to map your own:

```python
COURSE_FOLDERS = {
    "INCS104": "Data Structures and Algorithms",
    "INCS103": "Databases",
}
```

Folders are created next to the script. Set `PREFIX_WEEK = True` if you'd
rather have `Week 03 - Lecture 3.pdf`.

## Planned

**Notion checklist.** Mirror what gets downloaded into a Notion database — one
row per file, with its course, week and title, and a checkbox for whether you've
actually gone through it. The scraper already builds exactly this structure in
`_cms_manifest.json`, so the work is the Notion API integration, not the
bookkeeping. Not implemented yet.

**A GUI.** A small window instead of the terminal: tick the courses you want,
press Download, watch a progress bar, and see your saved login rather than
typing it again. Everything the flags do now, without anyone needing to know
what a flag is. Built with CustomTkinter — flat, rounded, dark-mode-aware
widgets on top of the Tkinter that already ships with Python, so it stays one
small pip install rather than a heavyweight toolkit. Not implemented yet.

## License

MIT — see [LICENSE](LICENSE).
