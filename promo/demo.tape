# LHGP — Deadline Contract Hub — terminal demo (1200x700, Catppuccin Mocha)
#
# Render with VHS (charmbracelet/vhs):
#   vhs promo/demo.tape
#
# NOTE (Windows): native VHS needs ttyd + ffmpeg on PATH and may hang while
# capturing (gdigrab). If `vhs` stalls, render inside WSL, or use the
# dependency-free fallback:  python promo/render_demo.py
# See promo/README.md for the full story.

Output promo/lhgp-demo.mp4

Set Shell bash
Set FontSize 20
Set Width 1200
Set Height 700
Set Theme "Catppuccin Mocha"
Set Padding 28
Set TypingSpeed 28ms
Set CursorBlink true

# --- neutral shell prompt, never reveal local paths -------------------------
Hide
Type "export PS1='lhgp $ '; export PYTHONUTF8=1; clear" Enter
Sleep 500ms
Show

# --- 1. doctor: preflight check, ALL SYSTEMS GO ------------------------------
Type "lhgp --data-dir promo/demo-data doctor" Enter
Sleep 3200ms

# --- 2. prepare: the protocol guards you (deadline needs a timezone) --------
Type "lhgp --data-dir promo/demo-data prepare --contract-id lt-20260905-demo --title \"Quarterly report\" --objective \"Publish Q3 report with reviewed financials\" --deadline \"2026-10-05T18:00:00\"" Enter
Sleep 2600ms

# --- 3. prepare again, well-formed: contract drafted -------------------------
Type "lhgp --data-dir promo/demo-data prepare --contract-id lt-20260905-demo --title \"Quarterly report\" --objective \"Publish Q3 report with reviewed financials\" --deadline \"2026-10-05T18:00:00+08:00\" --workload-hours 12" Enter
Sleep 3500ms

# --- 4. get: inspect the drafted contract ------------------------------------
Type "lhgp --data-dir promo/demo-data get lt-20260905-demo --decision-limit 1 --attempt-limit 1" Enter
Sleep 2800ms

# --- 5. approve: drafted -> active -------------------------------------------
Type "lhgp --data-dir promo/demo-data approve lt-20260905-demo" Enter
Sleep 2800ms

# --- 6. request-verification: acceptance request, audited as-is --------------
Type "lhgp --data-dir promo/demo-data request-verification lt-20260905-demo --reason \"Deliverables ready for acceptance review\"" Enter
Sleep 2800ms

# --- 7. watch: the auditable lifecycle event stream (core selling point) -----
# (dev note: `lhgp watch` currently drops --data-dir during dispatch, so the
#  demo tails through the module entry until that is fixed)
Type "python -m longtask.cli.watch --data-dir promo/demo-data --contract lt-20260905-demo --follow --for 8" Enter
Sleep 9000ms

# --- 8. closing line ----------------------------------------------------------
Ctrl+L
Sleep 600ms
Type "# Contracts that outlive the session. Deadlines that enforce themselves." Enter
Sleep 5500ms
