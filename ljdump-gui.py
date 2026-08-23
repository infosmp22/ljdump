#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# ljdump-gui.py - graphical front end for ljdump
# Greg Hewgill <greg@hewgill.com> https://hewgill.com
#
# LICENSE
#
# This software is provided 'as-is', without any express or implied
# warranty.  In no event will the author be held liable for any damages
# arising from the use of this software.
#
# Permission is granted to anyone to use this software for any purpose,
# including commercial applications, and to alter it and redistribute it
# freely, subject to the following restrictions:
#
# 1. The origin of this software must not be misrepresented; you must not
#    claim that you wrote the original software. If you use this software
#    in a product, an acknowledgment in the product documentation would be
#    appreciated but is not required.
# 2. Altered source versions must be plainly marked as such, and must not be
#    misrepresented as being the original software.
# 3. This notice may not be removed or altered from any source distribution.
#
# Copyright (c) 2005-2024 Greg Hewgill and contributors

import json
import os
import queue
import re
import subprocess
import sys
import threading
import traceback
import xml.dom.minidom
from datetime import datetime
from xml.sax import saxutils

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import ljdump
import ljdumptohtml


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = "ljdump.config"
PREFS_FILE = "ljdump-gui.json"

SERVERS = [
    "https://www.dreamwidth.org",
    "https://livejournal.com",
]

MODE_SYNC_AND_PAGES = "sync_and_pages"
MODE_SYNC_ONLY = "sync_only"
MODE_PAGES_ONLY = "pages_only"

# How many log lines to move into the text widget per pump, so that a fast run
# can't starve the event loop and freeze the window.
LOG_LINES_PER_PUMP = 200


class QueueWriter(object):
    """A file-like object that turns writes into tagged lines on a queue.

    The worker thread points sys.stdout and sys.stderr at one of these, so every
    print() already in ljdump.py, ljdumpsqlite.py and ljdumptohtml.py lands in the
    log pane without any of those files needing to know a GUI exists.

    It also sidesteps the UnicodeEncodeError that non-ASCII entry subjects cause
    when stdout is a pipe rather than a console, since nothing is ever encoded.
    """

    def __init__(self, log_queue, tag="info"):
        self.log_queue = log_queue
        self.tag = tag
        self.buffer = ""

    def write(self, text):
        if not text:
            return 0
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.log_queue.put((self.tag, line))
        return len(text)

    def flush(self):
        if self.buffer:
            self.log_queue.put((self.tag, self.buffer))
            self.buffer = ""

    def isatty(self):
        return False


def element_text(document, tag_name):
    """First text value for a tag in an ljdump.config document, or an empty string."""
    elements = document.documentElement.getElementsByTagName(tag_name)
    if not elements or not elements[0].childNodes:
        return ""
    return elements[0].childNodes[0].data


def read_config(folder):
    """Read an existing ljdump.config - the same XML file the command line uses."""
    path = os.path.join(folder, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    document = xml.dom.minidom.parse(path)
    journals = [e.childNodes[0].data
                for e in document.documentElement.getElementsByTagName("journal")
                if e.childNodes]
    return {
        "server": element_text(document, "server"),
        "username": element_text(document, "username"),
        "password": element_text(document, "password"),
        "ljuniq": element_text(document, "ljuniq"),
        "journals": journals,
    }


def write_config(folder, server, username, password, journals, ljuniq):
    """Write ljdump.config in the format ljdump.py itself reads, so running from
    the command line picks up exactly the same settings."""
    lines = ['<?xml version="1.0"?>', "<ljdump>"]
    lines.append("    <server>%s</server>" % saxutils.escape(server))
    lines.append("    <username>%s</username>" % saxutils.escape(username))
    if password:
        lines.append("    <password>%s</password>" % saxutils.escape(password))
    for journal in journals:
        lines.append("    <journal>%s</journal>" % saxutils.escape(journal))
    if ljuniq:
        lines.append("    <ljuniq>%s</ljuniq>" % saxutils.escape(ljuniq))
    lines.append("</ljdump>")
    path = os.path.join(folder, CONFIG_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def read_prefs():
    """GUI-only preferences, kept out of ljdump.config so that file stays
    compatible with the command line."""
    path = os.path.join(SCRIPT_DIR, PREFS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def write_prefs(prefs):
    path = os.path.join(SCRIPT_DIR, PREFS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)


def safe_filename_part(text):
    return re.sub(r'[^A-Za-z0-9_.-]', "_", text) or "journal"


class LjdumpGui(object):

    def __init__(self, root):
        self.root = root
        self.log_queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.log_file = None
        self.log_file_path = None
        self.close_when_done = False
        self.entry_count = 0
        self.comment_count = 0
        self.error_count = 0

        root.title("ljdump - journal backup")
        root.geometry("960x740")
        root.minsize(760, 560)

        # Even a bug in the GUI itself should end up visible rather than silently
        # killing a callback, which is the whole point of this front end.
        root.report_callback_exception = self.report_callback_exception
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.build_widgets()
        self.load_saved_settings()

        self.log("ljdump graphical front end.", "ok")
        self.log("Fill in your details and press Start. Everything that happens, "
                 "including any errors, is shown below and saved to a log file.")
        self.log("")

    # ------------------------------------------------------------------ widgets

    def build_widgets(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        self.build_settings(outer)
        self.build_options(outer)
        self.build_actions(outer)
        self.build_log(outer)
        self.build_status(outer)

    def build_settings(self, parent):
        frame = ttk.LabelFrame(parent, text="Account", padding=10)
        frame.pack(fill=tk.X)
        frame.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frame, text="Server:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.server_var = tk.StringVar(value=SERVERS[0])
        server_box = ttk.Combobox(frame, textvariable=self.server_var, values=SERVERS)
        server_box.grid(row=row, column=1, columnspan=2, sticky=tk.EW, pady=3)

        row += 1
        ttk.Label(frame, text="Username:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.username_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.username_var).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=3)

        row += 1
        ttk.Label(frame, text="Password:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.password_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.password_var, show="•").grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=3)

        row += 1
        self.remember_password_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Remember password",
                        variable=self.remember_password_var).grid(
            row=row, column=1, sticky=tk.W)
        ttk.Label(frame, text="stored as plain text in ljdump.config",
                  foreground="#a05000").grid(row=row, column=2, sticky=tk.W)

        row += 1
        ttk.Label(frame, text="Journal(s):").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.journals_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.journals_var).grid(
            row=row, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="blank for your own; comma separated for communities").grid(
            row=row, column=2, sticky=tk.W, padx=(8, 0))

        row += 1
        ttk.Label(frame, text="ljuniq cookie:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.ljuniq_var = tk.StringVar()
        self.ljuniq_entry = ttk.Entry(frame, textvariable=self.ljuniq_var, show="•")
        self.ljuniq_entry.grid(row=row, column=1, sticky=tk.EW, pady=3)
        ttk.Label(frame, text="only needed for caching Dreamwidth hosted images").grid(
            row=row, column=2, sticky=tk.W, padx=(8, 0))

        row += 1
        ttk.Label(frame, text="Backup folder:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.folder_var = tk.StringVar(value=SCRIPT_DIR)
        ttk.Entry(frame, textvariable=self.folder_var).grid(
            row=row, column=1, sticky=tk.EW, pady=3)
        ttk.Button(frame, text="Browse...", command=self.browse_folder).grid(
            row=row, column=2, sticky=tk.W, padx=(8, 0))

    def build_options(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=(10, 0))

        mode_frame = ttk.LabelFrame(frame, text="What to do", padding=10)
        mode_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.mode_var = tk.StringVar(value=MODE_SYNC_AND_PAGES)
        ttk.Radiobutton(mode_frame, text="Download from server, then build HTML pages",
                        variable=self.mode_var, value=MODE_SYNC_AND_PAGES).pack(anchor=tk.W)
        ttk.Radiobutton(mode_frame, text="Download from server only",
                        variable=self.mode_var, value=MODE_SYNC_ONLY).pack(anchor=tk.W)
        ttk.Radiobutton(mode_frame, text="Rebuild HTML pages only (no network)",
                        variable=self.mode_var, value=MODE_PAGES_ONLY).pack(anchor=tk.W)

        option_frame = ttk.LabelFrame(frame, text="Options", padding=10)
        option_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

        self.cache_images_var = tk.BooleanVar(value=False)
        self.cache_images_var.trace_add("write", self.update_ljuniq_state)
        ttk.Checkbutton(option_frame, text="Cache images from entries",
                        variable=self.cache_images_var).pack(anchor=tk.W)

        self.retry_images_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text="Retry images that failed before",
                        variable=self.retry_images_var).pack(anchor=tk.W)

        self.verbose_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(option_frame, text="Verbose logging",
                        variable=self.verbose_var).pack(anchor=tk.W)

        max_row = ttk.Frame(option_frame)
        max_row.pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(max_row, text="Max items per run:").pack(side=tk.LEFT)
        self.max_var = tk.StringVar(value="400")
        ttk.Spinbox(max_row, from_=1, to=100000, width=8,
                    textvariable=self.max_var).pack(side=tk.LEFT, padx=(6, 0))

    def build_actions(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=10)

        self.start_button = ttk.Button(frame, text="Start", command=self.start)
        self.start_button.pack(side=tk.LEFT)

        self.stop_button = ttk.Button(frame, text="Stop", command=self.stop,
                                      state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Button(frame, text="Save settings", command=self.save_settings).pack(
            side=tk.LEFT, padx=(20, 0))
        ttk.Button(frame, text="Open backup folder", command=self.open_folder).pack(
            side=tk.LEFT, padx=(6, 0))
        ttk.Button(frame, text="Save log as...", command=self.save_log_as).pack(
            side=tk.LEFT, padx=(6, 0))
        ttk.Button(frame, text="Clear log", command=self.clear_log).pack(
            side=tk.LEFT, padx=(6, 0))

    def build_log(self, parent):
        frame = ttk.LabelFrame(parent, text="Log", padding=6)
        frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = ScrolledText(frame, wrap=tk.WORD, height=18,
                                     font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_configure("info")
        self.log_text.tag_configure("ok", foreground="#0a7a30")
        self.log_text.tag_configure("warn", foreground="#a05000")
        self.log_text.tag_configure("error", foreground="#c00000",
                                    font=("Consolas", 9, "bold"))

    def build_status(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=(8, 0))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status_var).pack(side=tk.LEFT)

        self.counts_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.counts_var).pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=160)
        self.progress.pack(side=tk.RIGHT, padx=(0, 12))

    # ------------------------------------------------------------- settings i/o

    def update_ljuniq_state(self, *_args):
        self.ljuniq_entry.configure(
            state=(tk.NORMAL if self.cache_images_var.get() else tk.DISABLED))

    def load_saved_settings(self):
        prefs = read_prefs()
        folder = prefs.get("folder") or SCRIPT_DIR
        if not os.path.isdir(folder):
            folder = SCRIPT_DIR
        self.folder_var.set(folder)
        self.mode_var.set(prefs.get("mode", MODE_SYNC_AND_PAGES))
        self.max_var.set(str(prefs.get("max_to_fetch", 400)))
        self.cache_images_var.set(bool(prefs.get("cache_images", False)))
        self.retry_images_var.set(bool(prefs.get("retry_images", True)))
        self.verbose_var.set(bool(prefs.get("verbose", True)))

        try:
            config = read_config(folder)
        except Exception as x:
            config = {}
            self.log("Could not read %s: %s" % (CONFIG_FILE, x), "warn")

        if config.get("server"):
            self.server_var.set(config["server"])
        if config.get("username"):
            self.username_var.set(config["username"])
        if config.get("password"):
            self.password_var.set(config["password"])
            self.remember_password_var.set(True)
        if config.get("ljuniq"):
            self.ljuniq_var.set(config["ljuniq"])
        if config.get("journals"):
            self.journals_var.set(", ".join(config["journals"]))

        self.update_ljuniq_state()

    def save_settings(self):
        folder = self.folder_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("ljdump", "The backup folder does not exist:\n%s" % folder)
            return
        password = self.password_var.get() if self.remember_password_var.get() else ""
        try:
            path = write_config(folder,
                                self.server_var.get().strip(),
                                self.username_var.get().strip(),
                                password,
                                self.journal_list(),
                                self.ljuniq_var.get().strip())
            write_prefs({
                "folder": folder,
                "mode": self.mode_var.get(),
                "max_to_fetch": self.max_to_fetch(default=400),
                "cache_images": self.cache_images_var.get(),
                "retry_images": self.retry_images_var.get(),
                "verbose": self.verbose_var.get(),
            })
        except Exception as x:
            self.log("Could not save settings: %s" % x, "error")
            messagebox.showerror("ljdump", "Could not save settings:\n%s" % x)
            return
        self.log("Settings saved to %s" % path, "ok")
        if not password:
            self.log("The password was not saved. Tick 'Remember password' to store it.")

    def browse_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or SCRIPT_DIR,
                                         title="Choose a folder to keep backups in")
        if not chosen:
            return
        folder = os.path.normpath(chosen)
        self.folder_var.set(folder)

        # If that folder already holds an ljdump.config, use the details in it.
        try:
            config = read_config(folder)
        except Exception as x:
            self.log("Could not read %s in %s: %s" % (CONFIG_FILE, folder, x), "warn")
            return
        if not config:
            return
        if config.get("server"):
            self.server_var.set(config["server"])
        if config.get("username"):
            self.username_var.set(config["username"])
        if config.get("password"):
            self.password_var.set(config["password"])
            self.remember_password_var.set(True)
        if config.get("ljuniq"):
            self.ljuniq_var.set(config["ljuniq"])
        if config.get("journals"):
            self.journals_var.set(", ".join(config["journals"]))
        self.log("Loaded settings from %s" % os.path.join(folder, CONFIG_FILE), "ok")

    def open_folder(self):
        folder = self.folder_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("ljdump", "The backup folder does not exist:\n%s" % folder)
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as x:
            messagebox.showerror("ljdump", "Could not open the folder:\n%s" % x)

    def journal_list(self):
        raw = self.journals_var.get().strip()
        journals = [j.strip() for j in raw.split(",") if j.strip()]
        if not journals:
            username = self.username_var.get().strip()
            journals = [username] if username else []
        return journals

    def max_to_fetch(self, default=400):
        try:
            value = int(self.max_var.get())
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    # -------------------------------------------------------------------- log

    def log(self, text, tag="info"):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        if self.log_file is not None:
            try:
                self.log_file.write(text + "\n")
                # Flush every line, so even a hard crash or a closed window leaves
                # a complete record on disk.
                self.log_file.flush()
            except (OSError, ValueError):
                pass

    def classify(self, text, tag):
        """Colour ordinary output from the worker based on what it says."""
        if tag != "info":
            return tag
        stripped = text.strip()
        if (stripped.startswith("***") or stripped.startswith("Error")
                or stripped.startswith("Traceback") or stripped.startswith('File "')
                or stripped.startswith("Unexpected")):
            return "error"
        if (stripped.startswith("Stop requested") or stripped.startswith("Stopped")
                or stripped.startswith("Skipping") or stripped.startswith("No comment")):
            return "warn"
        if (stripped.startswith("Done!") or stripped.startswith("Created subdirectory")
                or " new entries," in stripped):
            return "ok"
        return "info"

    def update_counts(self, text):
        match = re.search(r'(\d+) new entries, (\d+) new comments', text)
        if match:
            self.entry_count += int(match.group(1))
            self.comment_count += int(match.group(2))
        match = re.match(r'^(\d+) errors$', text.strip())
        if match:
            self.error_count += int(match.group(1))
        self.counts_var.set("Entries: %d   Comments: %d   Errors: %d"
                            % (self.entry_count, self.comment_count, self.error_count))

    def clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def save_log_as(self):
        path = filedialog.asksaveasfilename(
            title="Save log", defaultextension=".txt",
            initialfile="ljdump-log.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_text.get("1.0", tk.END))
        except OSError as x:
            messagebox.showerror("ljdump", "Could not save the log:\n%s" % x)
            return
        self.log("Log saved to %s" % path, "ok")

    # ------------------------------------------------------------------ running

    def start(self):
        if self.worker is not None:
            return

        mode = self.mode_var.get()
        folder = self.folder_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()
        journals = self.journal_list()

        if not os.path.isdir(folder):
            messagebox.showerror("ljdump", "The backup folder does not exist:\n%s" % folder)
            return
        if not username:
            messagebox.showerror("ljdump", "Please enter your username.")
            return
        if not password and mode != MODE_PAGES_ONLY:
            messagebox.showerror("ljdump", "Please enter your password.")
            return
        if not journals:
            messagebox.showerror("ljdump", "Please enter at least one journal to back up.")
            return

        job = {
            "mode": mode,
            "folder": folder,
            "server": self.server_var.get().strip() or SERVERS[0],
            "username": username,
            "password": password,
            "journals": journals,
            "ljuniq": self.ljuniq_var.get().strip() or None,
            "max_to_fetch": self.max_to_fetch(),
            "cache_images": self.cache_images_var.get(),
            "retry_images": self.retry_images_var.get(),
            "verbose": self.verbose_var.get(),
        }

        # Remember the non-secret choices, so the next launch starts where this one
        # left off even if "Save settings" is never pressed.
        try:
            write_prefs({
                "folder": folder,
                "mode": mode,
                "max_to_fetch": job["max_to_fetch"],
                "cache_images": job["cache_images"],
                "retry_images": job["retry_images"],
                "verbose": job["verbose"],
            })
        except OSError:
            pass

        self.open_log_file(folder, journals[0])
        self.entry_count = 0
        self.comment_count = 0
        self.error_count = 0
        self.counts_var.set("")
        self.stop_event.clear()

        self.log("")
        self.log("=" * 72)
        self.log("Started %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok")
        self.log("Journals: %s" % ", ".join(journals))
        self.log("Folder:   %s" % folder)
        if self.log_file_path:
            self.log("Log file: %s" % self.log_file_path)
        self.log("=" * 72)

        self.start_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.NORMAL)
        self.status_var.set("Running...")
        self.progress.start(12)

        self.worker = threading.Thread(target=self.run_job, args=(job,))
        self.worker.daemon = True
        self.worker.start()
        self.root.after(100, self.pump)

    def open_log_file(self, folder, journal):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = "ljdump-log-%s-%s.txt" % (safe_filename_part(journal), stamp)
        path = os.path.join(folder, name)
        try:
            self.log_file = open(path, "w", encoding="utf-8", errors="replace")
            self.log_file_path = path
        except OSError as x:
            self.log_file = None
            self.log_file_path = None
            self.log("Could not open a log file in %s: %s" % (folder, x), "warn")

    def close_log_file(self):
        if self.log_file is not None:
            try:
                self.log_file.close()
            except OSError:
                pass
        self.log_file = None

    def run_job(self, job):
        """Runs on the worker thread. Anything it prints ends up in the log pane,
        and anything it raises is reported there rather than ending the process."""
        writer = QueueWriter(self.log_queue)
        error_writer = QueueWriter(self.log_queue, tag="error")
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = writer, error_writer
        outcome = "ok"
        try:
            # ljdump writes its output folder, database and config relative to the
            # working directory, so point that at the chosen backup folder.
            os.chdir(job["folder"])
            for journal in job["journals"]:
                if self.stop_event.is_set():
                    break
                if job["mode"] == MODE_PAGES_ONLY:
                    ljdumptohtml.ljdumptohtml(
                        username=job["username"],
                        journal_short_name=journal,
                        ljuniq=job["ljuniq"],
                        verbose=job["verbose"],
                        cache_images=job["cache_images"],
                        retry_images=job["retry_images"],
                        should_stop=self.stop_event.is_set,
                    )
                else:
                    ljdump.ljdump(
                        journal_server=job["server"],
                        username=job["username"],
                        password=job["password"],
                        journal_short_name=journal,
                        ljuniq=job["ljuniq"],
                        verbose=job["verbose"],
                        max_to_fetch=job["max_to_fetch"],
                        make_pages=(job["mode"] == MODE_SYNC_AND_PAGES),
                        cache_images=job["cache_images"],
                        retry_images=job["retry_images"],
                        should_stop=self.stop_event.is_set,
                    )
        except BaseException as x:
            outcome = "error:%s: %s" % (type(x).__name__, x)
            writer.flush()
            self.log_queue.put(("error", ""))
            self.log_queue.put(("error", "The run stopped because of an error:"))
            for line in traceback.format_exc().rstrip().split("\n"):
                self.log_queue.put(("error", line))
        finally:
            writer.flush()
            error_writer.flush()
            sys.stdout, sys.stderr = old_stdout, old_stderr
            self.log_queue.put(("__done__", outcome))

    def pump(self):
        """Move worker output into the text widget, a bounded batch at a time."""
        finished = None
        drained = 0
        try:
            while drained < LOG_LINES_PER_PUMP:
                tag, text = self.log_queue.get_nowait()
                drained += 1
                if tag == "__done__":
                    finished = text
                    break
                self.log(text, self.classify(text, tag))
                self.update_counts(text)
        except queue.Empty:
            pass

        if finished is None:
            self.root.after(100, self.pump)
            return

        # Drain whatever arrived after the done marker before reporting.
        while True:
            try:
                tag, text = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if tag != "__done__":
                self.log(text, self.classify(text, tag))
                self.update_counts(text)
        self.job_finished(finished)

    def job_finished(self, outcome):
        self.worker = None
        self.progress.stop()
        self.start_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)

        if outcome.startswith("error:"):
            message = outcome[len("error:"):]
            self.status_var.set("Stopped with an error.")
            self.log("")
            self.log("Finished with an error: %s" % message, "error")
            if self.log_file_path:
                self.log("The full log is saved at %s" % self.log_file_path)
            self.close_log_file()
            if self.close_when_done:
                self.root.destroy()
                return
            messagebox.showerror(
                "ljdump",
                "The run stopped because of an error:\n\n%s\n\n"
                "The full details are in the log below." % message)
            return

        if self.stop_event.is_set():
            self.status_var.set("Stopped.")
            self.log("")
            self.log("Stopped at your request. Everything fetched so far has been saved.",
                     "warn")
        else:
            self.status_var.set("Finished.")
            self.log("")
            self.log("Finished %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok")
        if self.log_file_path:
            self.log("The full log is saved at %s" % self.log_file_path)
        self.close_log_file()
        if self.close_when_done:
            self.root.destroy()

    def stop(self):
        if self.worker is None:
            return
        self.stop_event.set()
        self.stop_button.configure(state=tk.DISABLED)
        self.status_var.set("Stopping after the current item...")
        self.log("Stop requested. Finishing the current item, then saving.", "warn")

    # ------------------------------------------------------------------ closing

    def on_close(self):
        if self.worker is None:
            self.close_log_file()
            self.root.destroy()
            return
        if messagebox.askyesno(
                "ljdump",
                "A backup is still running.\n\n"
                "Stop it and close? The window will close once the current item "
                "finishes, and everything fetched so far will be saved."):
            self.close_when_done = True
            self.stop()

    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        for line in details.rstrip().split("\n"):
            self.log(line, "error")
        messagebox.showerror("ljdump", "Something went wrong in the interface:\n\n"
                                       "%s: %s" % (exc_type.__name__, exc_value))


def main():
    root = tk.Tk()
    LjdumpGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()

# vim:ts=4 et:
