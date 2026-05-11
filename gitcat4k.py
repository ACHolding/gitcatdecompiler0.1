#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
"""
GitCat 0.1 🐱
=============
Reverse-engineers a public GitHub repo into a synthetic "vibe coding" prompt.
ChatGPT-style tkinter HUD. Blue text on black buttons. Zero external deps.

Providers (auto-detected, override with LLM_PROVIDER):
    lmstudio    - default if a local LM Studio server is reachable
    openai      - if OPENAI_API_KEY set
    grok        - if GROK_API_KEY set
    openrouter  - if OPENROUTER_API_KEY set
    google      - if GOOGLE_API_KEY set
    mock        - always works, no network needed

Env vars (all optional, app runs fully mocked without them):
    GITHUB_TOKEN              - raises GitHub rate limit
    LMSTUDIO_URL              - default http://localhost:1234/v1
    LMSTUDIO_MODEL            - default whatever LM Studio has loaded
    OPENAI_API_KEY / GROK_API_KEY / OPENROUTER_API_KEY / GOOGLE_API_KEY
    LLM_PROVIDER              - force provider
    LLM_MODEL                 - override model name
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# ─────────────────────────────────────────────────────────────────────────────
# Palette - ChatGPT-ish layout, blue text, black buttons
# ─────────────────────────────────────────────────────────────────────────────
BG_DEEP       = "#0a0a0a"   # app background
BG_SIDE       = "#000000"   # sidebar
BG_PANEL      = "#111111"   # message panels
BG_INPUT      = "#1a1a1a"   # entry / chat input
BG_BTN        = "#000000"   # buttons
BG_BTN_HOVER  = "#1a1a1a"
BORDER        = "#1f1f1f"
BORDER_HOT    = "#2563eb"

BLUE_PRIMARY  = "#4a9eff"   # main blue text
BLUE_BRIGHT   = "#6bb4ff"
BLUE_DIM      = "#3a7fd6"
TEXT_MUTED    = "#6b7a90"
TEXT_FAINT    = "#3a4555"
ACCENT_OK     = "#4ade80"
ACCENT_WARN   = "#fbbf24"
ACCENT_ERR    = "#f87171"

HISTORY_PATH  = Path.home() / ".gitcat_history.json"
LMSTUDIO_DEFAULT_URL = os.environ.get("LMSTUDIO_URL", "http://localhost:1234/v1")
README_TRUNCATE = 8000

# ─────────────────────────────────────────────────────────────────────────────
# Repo input parsing
# ─────────────────────────────────────────────────────────────────────────────
REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?(?:#.*)?$",
    re.IGNORECASE,
)
REPO_SHORT_RE = re.compile(r"^([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$")


def parse_repo(raw: str) -> tuple[str, str]:
    """Return (owner, repo) or raise ValueError."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty input")
    m = REPO_URL_RE.match(s) or REPO_SHORT_RE.match(s)
    if not m:
        raise ValueError(f"can't parse repo: {raw!r}")
    return m.group(1), m.group(2)


# ─────────────────────────────────────────────────────────────────────────────
# GitHub fetch (stdlib only)
# ─────────────────────────────────────────────────────────────────────────────
def _gh_request(path: str, timeout: float = 12.0) -> Any:
    url = f"https://api.github.com{path}"
    req = urlrequest.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "gitcat/0.1",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urlerror.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(f"repo not found: {path}")
        if e.code in (403, 429):
            raise PermissionError(f"github rate limit / forbidden ({e.code})")
        raise


def fetch_github_data(owner: str, repo: str) -> dict[str, Any]:
    """Pull metadata + depth-1 tree + README, best-effort with fallbacks."""
    out: dict[str, Any] = {"owner": owner, "repo": repo, "online": True}

    try:
        meta = _gh_request(f"/repos/{owner}/{repo}")
        out["meta"] = {
            "full_name":       meta.get("full_name", f"{owner}/{repo}"),
            "description":     meta.get("description") or "",
            "language":        meta.get("language") or "Unknown",
            "topics":          meta.get("topics") or [],
            "stargazers":      meta.get("stargazers_count", 0),
            "forks":           meta.get("forks_count", 0),
            "default_branch":  meta.get("default_branch", "main"),
            "homepage":        meta.get("homepage") or "",
            "license":         (meta.get("license") or {}).get("spdx_id") or "",
            "size_kb":         meta.get("size", 0),
        }
    except FileNotFoundError:
        raise
    except Exception as e:
        out["online"] = False
        out["meta"] = _stub_meta(owner, repo)
        out["fetch_warning"] = f"github meta failed: {e}"

    branch = out["meta"].get("default_branch") or "main"

    if out["online"]:
        try:
            tree = _gh_request(f"/repos/{owner}/{repo}/contents?ref={branch}")
            if isinstance(tree, list):
                out["tree"] = [
                    {"name": e.get("name"), "type": e.get("type"), "size": e.get("size", 0)}
                    for e in tree
                ]
            else:
                out["tree"] = []
        except Exception as e:
            out["tree"] = _stub_tree()
            out["fetch_warning"] = f"github tree failed: {e}"
    else:
        out["tree"] = _stub_tree()

    if out["online"]:
        try:
            readme = _gh_request(f"/repos/{owner}/{repo}/readme")
            content = readme.get("content", "")
            if readme.get("encoding") == "base64":
                content = base64.b64decode(content).decode("utf-8", errors="replace")
            out["readme"] = content[:README_TRUNCATE]
        except Exception:
            out["readme"] = _stub_readme(owner, repo)
    else:
        out["readme"] = _stub_readme(owner, repo)

    return out


def _stub_meta(owner: str, repo: str) -> dict[str, Any]:
    return {
        "full_name": f"{owner}/{repo}",
        "description": "(offline / stubbed metadata)",
        "language": "Python",
        "topics": ["mock", "offline"],
        "stargazers": 0,
        "forks": 0,
        "default_branch": "main",
        "homepage": "",
        "license": "",
        "size_kb": 0,
    }


def _stub_tree() -> list[dict]:
    return [
        {"name": "README.md",        "type": "file", "size": 1200},
        {"name": "src",              "type": "dir",  "size": 0},
        {"name": "package.json",     "type": "file", "size": 800},
        {"name": ".gitignore",       "type": "file", "size": 100},
        {"name": "LICENSE",          "type": "file", "size": 1100},
    ]


def _stub_readme(owner: str, repo: str) -> str:
    return (
        f"# {repo}\n\n"
        "Offline stub README — the real network call didn't reach GitHub.\n"
        "GitCat is still generating a prompt from this stub so you can see the flow."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Provider routing
# ─────────────────────────────────────────────────────────────────────────────
def lmstudio_reachable(url: str = LMSTUDIO_DEFAULT_URL, timeout: float = 0.5) -> bool:
    try:
        parsed = urlparse.urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pick_provider() -> str:
    forced = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if forced:
        return forced
    if lmstudio_reachable():
        return "lmstudio"
    for key, name in (
        ("OPENAI_API_KEY",     "openai"),
        ("GROK_API_KEY",       "grok"),
        ("OPENROUTER_API_KEY", "openrouter"),
        ("GOOGLE_API_KEY",     "google"),
    ):
        if os.environ.get(key):
            return name
    return "mock"


SYSTEM_PROMPT = (
    "You write synthetic 'vibe coding' prompts: a single paragraph (or short "
    "list) of roughly 120 to 200 words, plain language, describing what to "
    "build so a coding model could recreate the repo's vibe and intent without "
    "seeing the code. Be concrete about UI shape, key features, and tech "
    "choices visible in the metadata. No preamble, no markdown headers."
)


def build_user_message(data: dict[str, Any]) -> str:
    meta = data["meta"]
    tree_lines = [
        f"  - {e['name']}{'/' if e['type'] == 'dir' else ''}"
        for e in data.get("tree", [])
    ]
    readme = (data.get("readme") or "").strip()
    return (
        f"Repository: {meta['full_name']}\n"
        f"Description: {meta['description']}\n"
        f"Primary language: {meta['language']}\n"
        f"Topics: {', '.join(meta['topics']) or '(none)'}\n"
        f"Homepage: {meta['homepage'] or '(none)'}\n"
        f"License: {meta['license'] or '(none)'}\n"
        f"Default branch: {meta['default_branch']}\n"
        f"Stars: {meta['stargazers']}  Forks: {meta['forks']}\n\n"
        f"Root tree (depth 1):\n" + "\n".join(tree_lines) + "\n\n"
        f"README (truncated to {README_TRUNCATE} chars):\n{readme}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# LLM callers
# ─────────────────────────────────────────────────────────────────────────────
def _post_json(url: str, payload: dict, headers: dict, timeout: float = 90.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=body, method="POST")
    for k, v in {"Content-Type": "application/json", **headers}.items():
        req.add_header(k, v)
    with urlrequest.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def call_lmstudio(user_msg: str) -> str:
    url = LMSTUDIO_DEFAULT_URL.rstrip("/") + "/chat/completions"
    model = os.environ.get("LMSTUDIO_MODEL", "local-model")
    data = _post_json(url, {
        "model": model,
        "temperature": 0.7,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
    }, headers={})
    return _extract_openai_text(data)


def call_openai_compatible(user_msg: str, base: str, key: str, model: str) -> str:
    data = _post_json(base.rstrip("/") + "/chat/completions", {
        "model": model,
        "temperature": 0.7,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
    }, headers={"Authorization": f"Bearer {key}"})
    return _extract_openai_text(data)


def call_google(user_msg: str) -> str:
    key = os.environ["GOOGLE_API_KEY"]
    model = os.environ.get("LLM_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    data = _post_json(url, {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 400},
    }, headers={})
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("empty model response")


def _extract_openai_text(data: dict) -> str:
    try:
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            raise RuntimeError("empty model response")
        return text
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("empty model response")


def generate_with_provider(provider: str, user_msg: str) -> str:
    if provider == "lmstudio":
        return call_lmstudio(user_msg)
    if provider == "openai":
        return call_openai_compatible(
            user_msg,
            base="https://api.openai.com/v1",
            key=os.environ["OPENAI_API_KEY"],
            model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        )
    if provider == "grok":
        return call_openai_compatible(
            user_msg,
            base="https://api.x.ai/v1",
            key=os.environ["GROK_API_KEY"],
            model=os.environ.get("LLM_MODEL", "grok-2-latest"),
        )
    if provider == "openrouter":
        return call_openai_compatible(
            user_msg,
            base="https://openrouter.ai/api/v1",
            key=os.environ["OPENROUTER_API_KEY"],
            model=os.environ.get("LLM_MODEL", "anthropic/claude-3.5-sonnet"),
        )
    if provider == "google":
        return call_google(user_msg)
    return mock_generate(user_msg)


# ─────────────────────────────────────────────────────────────────────────────
# Mock generator (works without any network)
# ─────────────────────────────────────────────────────────────────────────────
def mock_generate(user_msg: str) -> str:
    """Build a plausible 120-200 word vibe prompt from the structured input."""
    def grab(label: str) -> str:
        m = re.search(rf"^{re.escape(label)}:\s*(.+)$", user_msg, re.MULTILINE)
        return (m.group(1).strip() if m else "")

    full   = grab("Repository") or "owner/repo"
    desc   = grab("Description") or ""
    lang   = grab("Primary language") or "Python"
    topics = grab("Topics") or ""
    tree   = re.findall(r"^\s*-\s*(.+)$", user_msg, re.MULTILINE)

    files = [t.rstrip("/") for t in tree]
    has   = {f.lower() for f in files}
    is_node   = "package.json" in has
    is_py     = "requirements.txt" in has or "pyproject.toml" in has or "setup.py" in has
    is_rust   = "cargo.toml" in has
    is_go     = "go.mod" in has
    is_next   = any(n in has for n in ("next.config.js", "next.config.ts", "next.config.mjs"))
    is_docker = "dockerfile" in has or "docker-compose.yml" in has

    stack_bits = []
    if is_next:   stack_bits.append("Next.js")
    elif is_node: stack_bits.append("Node/TypeScript")
    if is_py:     stack_bits.append("Python")
    if is_rust:   stack_bits.append("Rust")
    if is_go:     stack_bits.append("Go")
    if is_docker: stack_bits.append("Docker")
    if not stack_bits:
        stack_bits.append(lang)
    stack = ", ".join(stack_bits)

    name = full.split("/")[-1]
    topic_phrase = f" The repo tags it as {topics}." if topics and topics != "(none)" else ""
    desc_phrase  = f" The author describes it as: \"{desc}\"." if desc else ""

    interesting = [f for f in files if f.lower() not in {
        ".gitignore", "license", "readme.md", ".github", ".env.example"
    }][:6]
    file_phrase = (
        f" The root contains {', '.join(interesting)}, which hints at the project's shape."
        if interesting else ""
    )

    body = (
        f"Build me a {stack} project that recreates the vibe of {name}.{desc_phrase}"
        f"{topic_phrase}{file_phrase} "
        f"Set up a clean entry point, a sensible folder structure for {lang}, and wire in "
        f"the standard tooling for this stack (package manager, linter, formatter, basic "
        f"tests). The UI or CLI should match what the README implies — keep it minimal but "
        f"functional, with clear states for loading, success, and failure. Add config via "
        f"environment variables where it makes sense, and document the run steps in a short "
        f"README. Don't overengineer — ship the smallest version that captures the project's "
        f"intent, then leave hooks for the obvious next features. When you finish, actually "
        f"run it end-to-end and verify the happy path works before calling it done."
    )

    words = body.split()
    if len(words) > 200:
        body = " ".join(words[:200]).rstrip(",.") + "."
    return body


# ─────────────────────────────────────────────────────────────────────────────
# In-flight dedupe + history
# ─────────────────────────────────────────────────────────────────────────────
class InFlight:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, threading.Event] = {}

    def claim(self, key: str) -> tuple[bool, threading.Event]:
        with self._lock:
            if key in self._jobs:
                return False, self._jobs[key]
            ev = threading.Event()
            self._jobs[key] = ev
            return True, ev

    def finish(self, key: str) -> None:
        with self._lock:
            ev = self._jobs.pop(key, None)
        if ev:
            ev.set()


def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_PATH.read_text("utf-8"))
    except Exception:
        return []


def save_history(items: list[dict]) -> None:
    try:
        HISTORY_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Tk widgets — ChatGPT-style HUD
# ─────────────────────────────────────────────────────────────────────────────
class FlatButton(tk.Label):
    """Black button with blue text + hover."""
    def __init__(self, parent, text, command, *, primary=False, **kw):
        self.command = command
        self.primary = primary
        super().__init__(
            parent,
            text=text,
            bg=BG_BTN,
            fg=BLUE_PRIMARY if primary else BLUE_DIM,
            font=("Helvetica", 11, "bold" if primary else "normal"),
            padx=14, pady=8,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=BORDER_HOT if primary else BORDER,
            highlightcolor=BORDER_HOT if primary else BORDER,
            **kw,
        )
        self.bind("<Enter>", lambda _e: self._hover(True))
        self.bind("<Leave>", lambda _e: self._hover(False))
        self.bind("<Button-1>", lambda _e: self._click())

    def _hover(self, on: bool) -> None:
        self.configure(
            bg=BG_BTN_HOVER if on else BG_BTN,
            fg=BLUE_BRIGHT if on else (BLUE_PRIMARY if self.primary else BLUE_DIM),
        )

    def _click(self) -> None:
        if str(self.cget("state")) == "disabled":
            return
        try:
            self.command()
        except Exception as e:
            messagebox.showerror("GitCat", f"button failed: {e}")


class SidebarItem(tk.Frame):
    def __init__(self, parent, label: str, on_click: Callable[[], None]):
        super().__init__(parent, bg=BG_SIDE, cursor="hand2")
        self.label = tk.Label(
            self, text=label, anchor="w",
            bg=BG_SIDE, fg=BLUE_DIM,
            font=("Helvetica", 10),
            padx=14, pady=8,
        )
        self.label.pack(fill="x")
        for w in (self, self.label):
            w.bind("<Enter>", lambda _e: self._hover(True))
            w.bind("<Leave>", lambda _e: self._hover(False))
            w.bind("<Button-1>", lambda _e: on_click())

    def _hover(self, on: bool) -> None:
        self.label.configure(
            bg=BG_BTN_HOVER if on else BG_SIDE,
            fg=BLUE_BRIGHT if on else BLUE_DIM,
        )
        self.configure(bg=BG_BTN_HOVER if on else BG_SIDE)


# ─────────────────────────────────────────────────────────────────────────────
# Main app
# ─────────────────────────────────────────────────────────────────────────────
class GitCatApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.inflight = InFlight()
        self.history = load_history()
        self.current_prompt = ""

        root.title("GitCat 🐱  ·  reverse-vibe-prompt")
        root.configure(bg=BG_DEEP)
        root.geometry("1180x760")
        root.minsize(880, 560)

        self._build_layout()
        self._refresh_provider_label()
        self._refresh_history_sidebar()
        self._show_empty_state()

    # ── layout ───────────────────────────────────────────────────────────
    def _build_layout(self) -> None:
        # Sidebar
        self.sidebar = tk.Frame(self.root, bg=BG_SIDE, width=260)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        head = tk.Frame(self.sidebar, bg=BG_SIDE)
        head.pack(fill="x", padx=14, pady=(16, 8))
        tk.Label(
            head, text="GitCat 🐱", bg=BG_SIDE, fg=BLUE_PRIMARY,
            font=("Helvetica", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            head, text="reverse-vibe-prompt", bg=BG_SIDE, fg=TEXT_MUTED,
            font=("Helvetica", 9),
        ).pack(anchor="w")

        FlatButton(
            self.sidebar, text="+ new prompt", command=self._show_empty_state, primary=True,
        ).pack(fill="x", padx=12, pady=(8, 12))

        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill="x", padx=12)

        # Tuple pady is valid on pack(), not on tk.Label (TclError: bad screen distance "14 4").
        tk.Label(
            self.sidebar, text="history",
            bg=BG_SIDE, fg=TEXT_MUTED, font=("Helvetica", 9, "bold"),
            anchor="w", padx=14,
        ).pack(fill="x", pady=(14, 4))

        # Scrollable history container
        hist_wrap = tk.Frame(self.sidebar, bg=BG_SIDE)
        hist_wrap.pack(fill="both", expand=True, padx=0)
        self.history_canvas = tk.Canvas(hist_wrap, bg=BG_SIDE, highlightthickness=0)
        self.history_canvas.pack(side="left", fill="both", expand=True)
        self.history_inner = tk.Frame(self.history_canvas, bg=BG_SIDE)
        self.history_window = self.history_canvas.create_window(
            (0, 0), window=self.history_inner, anchor="nw",
        )
        self.history_inner.bind(
            "<Configure>",
            lambda _e: self.history_canvas.configure(scrollregion=self.history_canvas.bbox("all")),
        )
        self.history_canvas.bind(
            "<Configure>",
            lambda e: self.history_canvas.itemconfigure(self.history_window, width=e.width),
        )

        # Footer in sidebar
        foot = tk.Frame(self.sidebar, bg=BG_SIDE)
        foot.pack(fill="x", side="bottom", pady=10, padx=12)
        self.provider_label = tk.Label(
            foot, text="", bg=BG_SIDE, fg=TEXT_MUTED,
            font=("Helvetica", 9), anchor="w", justify="left",
        )
        self.provider_label.pack(fill="x", pady=(0, 8))
        FlatButton(foot, text="clear history", command=self._clear_history).pack(fill="x")

        # Main area
        main = tk.Frame(self.root, bg=BG_DEEP)
        main.pack(side="left", fill="both", expand=True)

        # Top bar
        top = tk.Frame(main, bg=BG_DEEP, height=56)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(
            top, text="paste a github url or  owner/repo",
            bg=BG_DEEP, fg=TEXT_MUTED, font=("Helvetica", 10),
        ).pack(side="left", padx=20)
        self.status_label = tk.Label(
            top, text="ready", bg=BG_DEEP, fg=ACCENT_OK,
            font=("Helvetica", 10, "bold"),
        )
        self.status_label.pack(side="right", padx=20)

        tk.Frame(main, bg=BORDER, height=1).pack(fill="x")

        # Body (scrollable chat-like area)
        body_wrap = tk.Frame(main, bg=BG_DEEP)
        body_wrap.pack(fill="both", expand=True)

        self.body_canvas = tk.Canvas(body_wrap, bg=BG_DEEP, highlightthickness=0)
        self.body_canvas.pack(side="left", fill="both", expand=True)
        body_scroll = ttk.Scrollbar(body_wrap, orient="vertical", command=self.body_canvas.yview)
        body_scroll.pack(side="right", fill="y")
        self.body_canvas.configure(yscrollcommand=body_scroll.set)

        self.body = tk.Frame(self.body_canvas, bg=BG_DEEP)
        self.body_window = self.body_canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind(
            "<Configure>",
            lambda _e: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")),
        )
        self.body_canvas.bind(
            "<Configure>",
            lambda e: self.body_canvas.itemconfigure(self.body_window, width=e.width),
        )
        # Mouse wheel scroll (mac/win + linux)
        self.body_canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.body_canvas.bind_all("<Button-4>", lambda _e: self.body_canvas.yview_scroll(-3, "units"))
        self.body_canvas.bind_all("<Button-5>", lambda _e: self.body_canvas.yview_scroll(3, "units"))

        # Bottom: ChatGPT-style input dock
        dock_wrap = tk.Frame(main, bg=BG_DEEP)
        dock_wrap.pack(fill="x", padx=24, pady=(8, 18))
        dock = tk.Frame(
            dock_wrap, bg=BG_INPUT,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER_HOT,
        )
        dock.pack(fill="x")

        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(
            dock, textvariable=self.entry_var,
            bg=BG_INPUT, fg=BLUE_PRIMARY, insertbackground=BLUE_PRIMARY,
            relief="flat", font=("Helvetica", 13),
            highlightthickness=0, bd=0,
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=14, pady=12)
        self.entry.bind("<Return>", lambda _e: self._submit())
        self.entry.bind("<FocusIn>",  lambda _e: dock.configure(highlightbackground=BORDER_HOT))
        self.entry.bind("<FocusOut>", lambda _e: dock.configure(highlightbackground=BORDER))

        self.send_btn = FlatButton(dock, text="reverse →", command=self._submit, primary=True)
        self.send_btn.pack(side="right", padx=8, pady=6)

        tip = tk.Label(
            dock_wrap,
            text="enter to submit  ·  LM Studio @ localhost:1234 auto-detected  ·  no keys = mock mode",
            bg=BG_DEEP, fg=TEXT_FAINT, font=("Helvetica", 9),
        )
        tip.pack(anchor="w", pady=(6, 0))

        self.entry.focus_set()

    def _on_wheel(self, event) -> None:
        # Scroll body if pointer is over it
        w = self.root.winfo_containing(event.x_root, event.y_root)
        parent = w
        while parent is not None:
            if parent is self.body_canvas:
                step = int(-event.delta / 60) or (-1 if event.delta > 0 else 1)
                self.body_canvas.yview_scroll(step, "units")
                return
            parent = getattr(parent, "master", None)

    # ── states ───────────────────────────────────────────────────────────
    def _clear_body(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()

    def _show_empty_state(self) -> None:
        self._clear_body()
        self.entry_var.set("")
        self.current_prompt = ""

        tk.Frame(self.body, bg=BG_DEEP, height=80).pack()
        tk.Label(
            self.body, text="🐱", bg=BG_DEEP, fg=BLUE_PRIMARY, font=("Helvetica", 56),
        ).pack(pady=(20, 4))
        tk.Label(
            self.body, text="reverse-engineer any github repo",
            bg=BG_DEEP, fg=BLUE_PRIMARY, font=("Helvetica", 20, "bold"),
        ).pack()
        tk.Label(
            self.body, text="into a synthetic vibe-coding prompt",
            bg=BG_DEEP, fg=TEXT_MUTED, font=("Helvetica", 12),
        ).pack(pady=(2, 28))

        chips = tk.Frame(self.body, bg=BG_DEEP); chips.pack(pady=4)
        examples = [
            "filiksyos/gitreverse",
            "anthropics/anthropic-sdk-python",
            "microsoft/vscode",
        ]
        for ex in examples:
            FlatButton(chips, text=ex, command=lambda v=ex: self._fill_and_submit(v)).pack(
                side="left", padx=6,
            )
        self._set_status("ready", ACCENT_OK)

    def _fill_and_submit(self, value: str) -> None:
        self.entry_var.set(value)
        self._submit()

    def _set_status(self, text: str, color: str = BLUE_PRIMARY) -> None:
        self.status_label.configure(text=text, fg=color)

    def _refresh_provider_label(self) -> None:
        provider = pick_provider()
        lmst = "✓ on" if lmstudio_reachable() else "✗ off"
        self.provider_label.configure(
            text=f"provider: {provider}\nLM Studio: {lmst}",
        )

    # ── history ──────────────────────────────────────────────────────────
    def _refresh_history_sidebar(self) -> None:
        for w in self.history_inner.winfo_children():
            w.destroy()
        for entry in self.history[:40]:
            label = entry.get("repo", "(unknown)")
            SidebarItem(
                self.history_inner,
                label=label,
                on_click=lambda e=entry: self._restore_history(e),
            ).pack(fill="x", pady=1)

    def _restore_history(self, entry: dict) -> None:
        self.entry_var.set(entry["repo"])
        self._render_result(entry["repo"], entry["prompt"], entry.get("provider", "mock"), cached=True)

    def _push_history(self, repo_key: str, prompt: str, provider: str) -> None:
        self.history = [h for h in self.history if h.get("repo") != repo_key]
        self.history.insert(0, {
            "repo": repo_key,
            "prompt": prompt,
            "provider": provider,
            "ts": int(time.time()),
        })
        self.history = self.history[:60]
        save_history(self.history)
        self._refresh_history_sidebar()

    def _clear_history(self) -> None:
        if not self.history:
            return
        if messagebox.askyesno("GitCat", "clear all history?"):
            self.history = []
            save_history(self.history)
            self._refresh_history_sidebar()

    # ── submit pipeline ──────────────────────────────────────────────────
    def _submit(self) -> None:
        raw = self.entry_var.get().strip()
        if not raw:
            self._set_status("paste a repo first", ACCENT_WARN)
            return
        try:
            owner, repo = parse_repo(raw)
        except ValueError as e:
            self._set_status(f"bad input · {e}", ACCENT_ERR)
            return

        key = f"{owner}/{repo}".lower()
        cached = next((h for h in self.history if h.get("repo", "").lower() == key), None)
        if cached:
            self._render_result(f"{owner}/{repo}", cached["prompt"], cached.get("provider", "?"), cached=True)
            return

        is_owner, _ev = self.inflight.claim(key)
        if not is_owner:
            self._set_status(f"already cooking {key}…", ACCENT_WARN)
            return

        self._render_loading(f"{owner}/{repo}")
        self.send_btn.configure(state="disabled")
        threading.Thread(
            target=self._run_pipeline,
            args=(owner, repo, key),
            daemon=True,
        ).start()

    def _run_pipeline(self, owner: str, repo: str, key: str) -> None:
        try:
            self._tk_status("fetching github metadata…", BLUE_PRIMARY)
            data = fetch_github_data(owner, repo)

            self._tk_status("assembling context…", BLUE_PRIMARY)
            user_msg = build_user_message(data)

            provider = pick_provider()
            self._tk_status(f"calling {provider}…", BLUE_PRIMARY)

            try:
                prompt = generate_with_provider(provider, user_msg)
            except (urlerror.URLError, urlerror.HTTPError, ConnectionError, socket.timeout, OSError) as e:
                self._tk_status(f"{provider} unreachable, falling back to mock", ACCENT_WARN)
                provider = "mock"
                prompt = mock_generate(user_msg)
            except RuntimeError as e:
                self._tk_status(f"{provider}: {e}, falling back to mock", ACCENT_WARN)
                provider = "mock"
                prompt = mock_generate(user_msg)

            if not prompt.strip():
                raise RuntimeError("empty model response")

            self.inflight.finish(key)
            self._tk_call(self._on_pipeline_ok, owner, repo, prompt, provider, data.get("fetch_warning"))
        except FileNotFoundError:
            self.inflight.finish(key)
            self._tk_call(self._on_pipeline_err, f"404 · repo not found: {owner}/{repo}")
        except PermissionError:
            self.inflight.finish(key)
            self._tk_call(self._on_pipeline_err, "429 · github rate limit — set GITHUB_TOKEN or wait")
        except Exception as e:
            self.inflight.finish(key)
            self._tk_call(self._on_pipeline_err, f"500 · {e}")

    def _on_pipeline_ok(self, owner: str, repo: str, prompt: str, provider: str, warning: str | None) -> None:
        self.send_btn.configure(state="normal")
        self._push_history(f"{owner}/{repo}", prompt, provider)
        self._render_result(f"{owner}/{repo}", prompt, provider, cached=False, warning=warning)

    def _on_pipeline_err(self, msg: str) -> None:
        self.send_btn.configure(state="normal")
        self._set_status(msg, ACCENT_ERR)
        self._clear_body()
        tk.Frame(self.body, bg=BG_DEEP, height=80).pack()
        tk.Label(
            self.body, text="✖", bg=BG_DEEP, fg=ACCENT_ERR, font=("Helvetica", 40),
        ).pack(pady=(20, 4))
        tk.Label(
            self.body, text=msg, bg=BG_DEEP, fg=BLUE_PRIMARY,
            font=("Helvetica", 13), wraplength=720, justify="center",
        ).pack(pady=8)

    def _tk_call(self, fn, *args) -> None:
        self.root.after(0, lambda: fn(*args))

    def _tk_status(self, text: str, color: str) -> None:
        self.root.after(0, lambda: self._set_status(text, color))

    # ── rendering ────────────────────────────────────────────────────────
    def _render_loading(self, repo: str) -> None:
        self._clear_body()
        self._set_status(f"reversing {repo}…", BLUE_PRIMARY)
        tk.Frame(self.body, bg=BG_DEEP, height=60).pack()
        tk.Label(
            self.body, text="◐", bg=BG_DEEP, fg=BLUE_PRIMARY, font=("Helvetica", 40),
        ).pack()
        tk.Label(
            self.body, text=f"reversing {repo}",
            bg=BG_DEEP, fg=BLUE_PRIMARY, font=("Helvetica", 14, "bold"),
        ).pack(pady=(10, 4))
        tk.Label(
            self.body, text="working…",
            bg=BG_DEEP, fg=TEXT_MUTED, font=("Helvetica", 11),
        ).pack()

    def _render_result(self, repo: str, prompt: str, provider: str, *, cached: bool, warning: str | None = None) -> None:
        self.current_prompt = prompt
        self._clear_body()
        self._set_status(
            f"{'cached · ' if cached else ''}done · {provider}",
            ACCENT_OK if not warning else ACCENT_WARN,
        )

        tk.Frame(self.body, bg=BG_DEEP, height=22).pack()
        head = tk.Frame(self.body, bg=BG_DEEP)
        head.pack(fill="x", padx=24)
        tk.Label(
            head, text=repo, bg=BG_DEEP, fg=BLUE_PRIMARY,
            font=("Helvetica", 18, "bold"),
        ).pack(side="left")
        tk.Label(
            head,
            text=f"  ·  {provider}{'  ·  cached' if cached else ''}",
            bg=BG_DEEP, fg=TEXT_MUTED, font=("Helvetica", 11),
        ).pack(side="left")
        FlatButton(head, text="copy", command=self._copy, primary=True).pack(side="right")

        if warning:
            tk.Label(
                self.body, text=f"⚠ {warning}",
                bg=BG_DEEP, fg=ACCENT_WARN, font=("Helvetica", 10),
                anchor="w",
            ).pack(fill="x", padx=24, pady=(8, 0))

        panel = tk.Frame(
            self.body, bg=BG_PANEL,
            highlightthickness=1, highlightbackground=BORDER,
        )
        panel.pack(fill="both", expand=True, padx=24, pady=(14, 10))

        txt = tk.Text(
            panel,
            bg=BG_PANEL, fg=BLUE_PRIMARY, insertbackground=BLUE_PRIMARY,
            relief="flat", wrap="word", padx=18, pady=16,
            font=("Helvetica", 12), height=14, bd=0,
            highlightthickness=0, selectbackground=BLUE_DIM, selectforeground="#000000",
        )
        txt.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(panel, orient="vertical", command=txt.yview)
        sb.pack(side="right", fill="y")
        txt.configure(yscrollcommand=sb.set)

        self._render_markdown_into(txt, prompt)
        txt.configure(state="disabled")

        wc = len(prompt.split())
        tk.Label(
            self.body, text=f"~{wc} words",
            bg=BG_DEEP, fg=TEXT_FAINT, font=("Helvetica", 9),
            anchor="e",
        ).pack(fill="x", padx=24, pady=(0, 18))

    def _render_markdown_into(self, txt: tk.Text, md: str) -> None:
        """Lightweight markdown: # headers, **bold**, `code`, - bullets."""
        txt.tag_configure("h1", font=("Helvetica", 16, "bold"), foreground=BLUE_BRIGHT, spacing3=8)
        txt.tag_configure("h2", font=("Helvetica", 14, "bold"), foreground=BLUE_BRIGHT, spacing3=6)
        txt.tag_configure("h3", font=("Helvetica", 12, "bold"), foreground=BLUE_BRIGHT, spacing3=4)
        txt.tag_configure("bold", font=("Helvetica", 12, "bold"), foreground=BLUE_BRIGHT)
        txt.tag_configure("code", font=("Menlo", 11), background="#000000", foreground=BLUE_BRIGHT)
        txt.tag_configure("bullet", lmargin1=20, lmargin2=36)

        for line in md.splitlines():
            stripped = line.lstrip()
            tag = None
            content = line
            if stripped.startswith("### "):
                tag, content = "h3", stripped[4:]
            elif stripped.startswith("## "):
                tag, content = "h2", stripped[3:]
            elif stripped.startswith("# "):
                tag, content = "h1", stripped[2:]
            elif stripped.startswith(("- ", "* ")):
                tag, content = "bullet", "•  " + stripped[2:]
            self._insert_inline(txt, content + "\n", base_tag=tag)

    def _insert_inline(self, txt: tk.Text, content: str, *, base_tag: str | None) -> None:
        pattern = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")
        idx = 0
        for m in pattern.finditer(content):
            if m.start() > idx:
                txt.insert("end", content[idx:m.start()], (base_tag,) if base_tag else ())
            chunk = m.group(0)
            if chunk.startswith("**"):
                tags = ("bold",) + ((base_tag,) if base_tag else ())
                txt.insert("end", chunk[2:-2], tags)
            else:
                tags = ("code",) + ((base_tag,) if base_tag else ())
                txt.insert("end", chunk[1:-1], tags)
            idx = m.end()
        if idx < len(content):
            txt.insert("end", content[idx:], (base_tag,) if base_tag else ())

    # ── copy ─────────────────────────────────────────────────────────────
    def _copy(self) -> None:
        if not self.current_prompt:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.current_prompt)
        self.root.update()
        self._set_status("copied to clipboard", ACCENT_OK)


# ─────────────────────────────────────────────────────────────────────────────
# Tiny self-test (only runs with --selftest, no GUI)
# ─────────────────────────────────────────────────────────────────────────────
def _selftest() -> int:
    checks: list[tuple[str, bool]] = []
    try:
        assert parse_repo("https://github.com/foo/bar") == ("foo", "bar")
        assert parse_repo("foo/bar") == ("foo", "bar")
        assert parse_repo("https://github.com/foo/bar.git") == ("foo", "bar")
        assert parse_repo("github.com/foo/bar") == ("foo", "bar")
        checks.append(("parse_repo accepts variants", True))
    except Exception as e:
        checks.append((f"parse_repo: {e}", False))

    try:
        parse_repo("not a repo")
        checks.append(("parse_repo rejects bad input", False))
    except ValueError:
        checks.append(("parse_repo rejects bad input", True))

    try:
        msg = build_user_message({
            "meta": _stub_meta("foo", "bar"),
            "tree": _stub_tree(),
            "readme": _stub_readme("foo", "bar"),
        })
        out = mock_generate(msg)
        wc = len(out.split())
        checks.append((f"mock_generate words={wc}", 80 <= wc <= 210))
    except Exception as e:
        checks.append((f"mock_generate: {e}", False))

    try:
        # Provider routing without env vars => mock
        for k in ("LLM_PROVIDER", "OPENAI_API_KEY", "GROK_API_KEY",
                  "OPENROUTER_API_KEY", "GOOGLE_API_KEY"):
            os.environ.pop(k, None)
        # lmstudio_reachable should be False in this env
        p = pick_provider()
        checks.append((f"pick_provider fallback => {p}", p in ("mock", "lmstudio")))
    except Exception as e:
        checks.append((f"pick_provider: {e}", False))

    ok = True
    for name, passed in checks:
        print(("✓ " if passed else "✗ ") + name)
        ok = ok and passed
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    root = tk.Tk()
    try:
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family="Helvetica", size=11)
    except Exception:
        pass
    GitCatApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
