"""LHGP promo video renderer (Windows-native fallback for `vhs promo/demo.tape`).

Pipeline:
  1. reset promo/demo-data (via the junction it maps to C:\\lhgp-demo-data)
  2. run the real CLI commands, capture their real stdout
  3. replay them in a pyte terminal emulator, rendering frames with Pillow
  4. encode frames to promo/lhgp-demo.mp4 with ffmpeg (concat demuxer)

Run from the repo root:
    PYTHONPATH=~/vhs-tools/pylibs .venv/Scripts/python.exe promo/render_demo.py
ffmpeg must be resolvable (PATH or WinGet Links).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
PROMO = ROOT / "promo"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
LHGP = ROOT / ".venv" / "Scripts" / "lhgp.exe"
DATA_DIR = "promo/demo-data"
OUT_MP4 = PROMO / "lhgp-demo.mp4"

# ---------------------------------------------------------------- canvas ----
W, H = 1200, 700
BAR_H = 40
PAD_X, PAD_Y = 26, 20
FONT_SIZE = 20
CELL_H = 30

BG = (0x1E, 0x1E, 0x2E)  # Catppuccin Mocha base
BAR_BG = (0x31, 0x32, 0x44)  # surface0
FG = (0xCD, 0xD6, 0xF4)  # text
GREEN = (0xA6, 0xE3, 0xA1)
RED = (0xF3, 0x8B, 0xA8)
YELLOW = (0xF9, 0xE2, 0xAF)
MAUVE = (0xCB, 0xA6, 0xF7)
CYAN = (0x94, 0xE2, 0xD5)
DIM = (0x6C, 0x70, 0x86)  # overlay0
DOTS = [(0xF3, 0x8B, 0xA8), (0xF9, 0xE2, 0xAF), (0xA6, 0xE3, 0xA1)]
TITLE = "lhgp \u2014 deadline contract hub"
SLOGAN_1 = "Contracts that outlive the session."
SLOGAN_2 = "Deadlines that enforce themselves."
FOOTER = "lhgp \u00b7 deadline contract hub"

FPS = 30

NAMED = {
    "green": GREEN,
    "red": RED,
    "yellow": YELLOW,
    "cyan": CYAN,
    "magenta": MAUVE,
    "blue": (0x89, 0xB4, 0xFA),
    "white": (0xFF, 0xFF, 0xFF),
    "black": (0x00, 0x00, 0x00),
    "brown": (0xFA, 0xB3, 0x87),
    "grey": DIM,
    "default": FG,
}


def find_font(names):
    for n in names:
        p = Path("C:/Windows/Fonts") / n
        if p.exists():
            return str(p)
    raise SystemExit(f"font not found: {names}")


FONT_PATH = find_font(["consola.ttf"])
FONT_BOLD_PATH = find_font(["consolab.ttf"])
FONT_UI_PATH = find_font(["segoeui.ttf"])
FONT_CJK_PATH = find_font(["msyh.ttc", "simsun.ttc"])

_font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
CELL_W = max(1, round(_font.getlength("M")))
COLS = (W - 2 * PAD_X) // CELL_W
ROWS = (H - BAR_H - 2 * PAD_Y) // CELL_H


# ------------------------------------------------------------- terminal -----
class Session:
    def __init__(self):
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.Stream(self.screen)

    def type_char(self, ch):
        self.stream.feed(ch)

    def enter(self):
        self.stream.feed("\r\n")

    def prompt(self):
        self.stream.feed("\x1b[32mlhgp\x1b[0m \x1b[36m$\x1b[0m ")

    def feed(self, text):
        # pyte treats bare LF as "move down, keep column" — normalize to CRLF
        self.stream.feed(text.replace("\n", "\r\n"))

    def clear(self):
        self.stream.feed("\x1b[2J\x1b[H")


def colorize_cli_output(text: str) -> str:
    """Cosmetic ANSI tinting of the CLI's plain output (as a TTY run would show)."""
    text = text.replace("[PASS]", "\x1b[32m[PASS]\x1b[0m")
    text = text.replace("[FAIL]", "\x1b[31m[FAIL]\x1b[0m")
    text = text.replace("ALL SYSTEMS GO", "\x1b[32m\x1b[1mALL SYSTEMS GO\x1b[0m")
    text = re.sub(r"^(Error \[)", "\x1b[31m\\1", text, flags=re.M)
    text = text.replace(
        "deadline_at must carry an explicit timezone",
        "deadline_at must carry an explicit timezone\x1b[0m",
    )
    text = text.replace("[watch]", "\x1b[36m[watch]\x1b[0m")
    return text


# ------------------------------------------------------------ demo steps ----
PREPARE_REJECT = (
    "lhgp --data-dir promo/demo-data prepare --contract-id lt-20260905-demo "
    '--title "Quarterly report" --objective "Publish Q3 report with reviewed '
    'financials" --deadline "2026-10-05T18:00:00"'
)
PREPARE_OK = (
    "lhgp --data-dir promo/demo-data prepare --contract-id lt-20260905-demo "
    '--title "Quarterly report" --objective "Publish Q3 report with reviewed '
    'financials" --deadline "2026-10-05T18:00:00+08:00" --workload-hours 12'
)
WATCH_CMD = (
    "python -m longtask.cli.watch --data-dir promo/demo-data "
    "--contract lt-20260905-demo --follow --for 6"
)


def run(args) -> str:
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run(
        args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    return (p.stdout + p.stderr).replace("\r\n", "\n").rstrip("\n")


def build_outputs():
    """Run the real demo sequence against a freshly reset demo data dir."""
    dd = ROOT / DATA_DIR
    dd.mkdir(parents=True, exist_ok=True)
    for child in dd.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    base = [str(LHGP), "--data-dir", DATA_DIR]
    outs = []
    outs.append(run([*base, "doctor"]))
    outs.append(
        run(
            [
                *base,
                "prepare",
                "--contract-id",
                "lt-20260905-demo",
                "--title",
                "Quarterly report",
                "--objective",
                "Publish Q3 report with reviewed financials",
                "--deadline",
                "2026-10-05T18:00:00",
            ]
        )
    )
    outs.append(
        run(
            [
                *base,
                "prepare",
                "--contract-id",
                "lt-20260905-demo",
                "--title",
                "Quarterly report",
                "--objective",
                "Publish Q3 report with reviewed financials",
                "--deadline",
                "2026-10-05T18:00:00+08:00",
                "--workload-hours",
                "12",
            ]
        )
    )
    outs.append(
        run([*base, "get", "lt-20260905-demo", "--decision-limit", "1", "--attempt-limit", "1"])
    )
    outs.append(run([*base, "approve", "lt-20260905-demo"]))
    outs.append(
        run(
            [
                *base,
                "request-verification",
                "lt-20260905-demo",
                "--reason",
                "Deliverables ready for acceptance review",
            ]
        )
    )
    outs.append(
        run(
            [
                str(VENV_PY),
                "-m",
                "longtask.cli.watch",
                "--data-dir",
                DATA_DIR,
                "--contract",
                "lt-20260905-demo",
                "--follow",
                "--for",
                "6",
            ]
        )
    )
    return outs


# ------------------------------------------------------------- rendering ----
def cell_color(ch, default):
    fg = ch.fg
    if not fg or fg == "default":
        return default
    if fg in NAMED:
        return NAMED[fg]
    if isinstance(fg, str) and fg.isdigit():
        return FG  # 256-palette: keep base (demo output does not use it)
    if isinstance(fg, str) and re.fullmatch(r"[0-9a-fA-F]{6}", fg or ""):
        return tuple(int(fg[i : i + 2], 16) for i in (0, 2, 4))
    return default


def render_terminal(
    sess: Session, cursor: bool = True, slogan: bool = False, slogan_phase: int = 0
) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # window bar with rounded top corners
    d.rounded_rectangle([0, 0, W - 1, BAR_H + 24], radius=14, fill=BAR_BG)
    d.rectangle([0, BAR_H, W - 1, BAR_H + 24], fill=BAR_BG)
    d.rectangle([0, BAR_H + 24, W - 1, BAR_H + 26], fill=(0x18, 0x18, 0x25))
    for i, c in enumerate(DOTS):
        cx = 30 + i * 26
        d.ellipse([cx - 7, BAR_H // 2 - 7, cx + 7, BAR_H // 2 + 7], fill=c)
    f_ui = ImageFont.truetype(FONT_UI_PATH, 14)
    tw = d.textlength(TITLE, font=f_ui)
    d.text(((W - tw) / 2, BAR_H / 2 - 9), TITLE, font=f_ui, fill=(0xA6, 0xAD, 0xC8))

    if slogan:
        f_big = ImageFont.truetype(FONT_BOLD_PATH, 30)
        f_small = ImageFont.truetype(FONT_PATH, 16)
        y1 = H / 2 - 52
        t1, t2 = SLOGAN_1, SLOGAN_2
        w1 = d.textlength(t1, font=f_big)
        w2 = d.textlength(t2, font=f_big)
        d.text(((W - w1) / 2, y1), t1, font=f_big, fill=FG)
        d.text(((W - w2) / 2, y1 + 48), t2, font=f_big, fill=MAUVE)
        if slogan_phase >= 1:
            wf = d.textlength(FOOTER, font=f_small)
            d.text(((W - wf) / 2, y1 + 118), FOOTER, font=f_small, fill=DIM)
        return img

    f_cjk = ImageFont.truetype(FONT_CJK_PATH, FONT_SIZE - 1)
    x0, y0 = PAD_X, BAR_H + 26 + PAD_Y
    for r, line in enumerate(sess.screen.display):
        y = y0 + r * CELL_H
        for c, ch in enumerate(line):
            if ch == " ":
                continue
            wide = unicodedata.east_asian_width(ch) in ("W", "F")
            color = cell_color(sess.screen.buffer[r][c], FG)
            px = x0 + c * CELL_W
            if wide:
                d.text((px, y + 2), ch, font=f_cjk, fill=color)
            else:
                d.text((px, y + 3), ch, font=_font, fill=color)

    if cursor:
        cx = x0 + sess.screen.cursor.x * CELL_W
        cy = y0 + min(sess.screen.cursor.y, ROWS - 1) * CELL_H
        d.rectangle([cx, cy + 2, cx + CELL_W - 1, cy + CELL_H - 2], fill=FG)
    return img


# -------------------------------------------------------------- timeline ----
class Encoder:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lhgp-frames-"))
        self.n = 0
        self.entries = []  # (png_name, frames)

    def push(self, img: Image.Image, seconds: float = 1 / FPS, frames: int | None = None):
        self.n += 1
        name = "f%06d.png" % self.n
        img.save(self.dir / name)
        k = frames if frames is not None else max(1, round(seconds * FPS))
        self.entries.append((name, k))

    def hold(self, seconds):
        k = max(1, round(seconds * FPS))
        if self.entries:
            name, _ = self.entries[-1]
            self.entries.append((name, k))
        return k

    def encode(self, out: Path):
        lst = self.dir / "list.txt"
        with open(lst, "w", newline="\n") as f:
            f.write("ffconcat version 1.0\n")
            for name, k in self.entries:
                dur = k / FPS
                f.write(f"file '{name}'\nduration {dur:.4f}\n")
            f.write(f"file '{self.entries[-1][0]}'\nduration {1 / FPS:.4f}\n")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links/ffmpeg.exe"
            if cand.exists():
                ffmpeg = str(cand)
        if not ffmpeg:
            raise SystemExit("ffmpeg not found")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-vsync",
                "cfr",
                "-r",
                str(FPS),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
                str(out),
            ],
            check=True,
            capture_output=True,
        )


def main():
    print("running demo commands...")
    outs = build_outputs()

    sess = Session()
    enc = Encoder()

    def type_line(cmd):
        for chch in cmd:
            sess.type_char(chch)
            enc.push(render_terminal(sess), frames=1)
        sess.enter()
        enc.push(render_terminal(sess), frames=6)

    def show_output(text, hold):
        sess.feed(colorize_cli_output(text) + "\r\n")
        enc.push(render_terminal(sess), frames=1)
        sess.prompt()
        enc.push(render_terminal(sess), frames=1)
        enc.hold(hold)

    # opening
    sess.prompt()
    enc.push(render_terminal(sess), frames=1)
    enc.hold(0.8)

    # 1. doctor
    type_line("lhgp --data-dir promo/demo-data doctor")
    show_output(outs[0], 3.2)

    # 2. prepare rejected (no timezone)
    type_line(PREPARE_REJECT)
    show_output(outs[1], 2.6)

    # 3. prepare ok
    type_line(PREPARE_OK)
    show_output(outs[2], 3.2)

    # 4. get
    type_line(
        "lhgp --data-dir promo/demo-data get lt-20260905-demo --decision-limit 1 --attempt-limit 1"
    )
    show_output(outs[3], 2.6)

    # 5. approve
    type_line("lhgp --data-dir promo/demo-data approve lt-20260905-demo")
    show_output(outs[4], 2.6)

    # 6. request-verification
    type_line(
        "lhgp --data-dir promo/demo-data request-verification "
        'lt-20260905-demo --reason "Deliverables ready for acceptance review"'
    )
    show_output(outs[5], 2.6)

    # 7. watch: reveal the event stream line by line
    type_line(WATCH_CMD)
    lines = outs[6].split("\n")
    for _i, ln in enumerate(lines):
        sess.feed(colorize_cli_output(ln) + "\r\n")
        enc.push(render_terminal(sess), frames=1)
        enc.hold(1.1 if not ln.startswith("[watch idle") else 0.9)
    sess.prompt()
    enc.push(render_terminal(sess), frames=1)
    enc.hold(1.0)

    # 8. closing slogan
    sess.clear()
    enc.push(render_terminal(sess), frames=1)
    enc.hold(0.6)
    enc.push(render_terminal(sess, cursor=False, slogan=True, slogan_phase=0), frames=1)
    enc.hold(3.4)
    enc.push(render_terminal(sess, cursor=False, slogan=True, slogan_phase=1), frames=1)
    enc.hold(2.6)

    print(
        "encoding %d clips (~%.1f s)..." % (len(enc.entries), sum(k for _, k in enc.entries) / FPS)
    )
    enc.encode(OUT_MP4)
    shutil.rmtree(enc.dir, ignore_errors=True)
    size = OUT_MP4.stat().st_size
    print(f"wrote {OUT_MP4} ({size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
