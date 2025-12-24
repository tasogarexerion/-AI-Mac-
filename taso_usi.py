#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import time
import re
from typing import List, Optional

ENGINE_CMD_DEFAULT = os.path.expanduser(
    "~/shogi/wrapper/taso_engine_fukaura.sh"
)

ENGINE_TIMEOUT_SEC = float(os.environ.get("TASO_ENGINE_TIMEOUT", "3.0"))
DEBUG = os.environ.get("TASO_DEBUG", "0") == "1"


# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------

def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def debug(msg: str) -> None:
    if DEBUG:
        sys.stderr.write("[TASO_USI] " + msg + "\n")
        sys.stderr.flush()


def safe_readlines(proc: subprocess.Popen, timeout: float) -> List[str]:
    """
    Read all remaining stdout lines with timeout.
    """
    lines: List[str] = []
    start = time.time()

    while True:
        if time.time() - start > timeout:
            break
        line = proc.stdout.readline()  # type: ignore
        if not line:
            break
        lines.append(line.rstrip("\n"))
    return lines


# ------------------------------------------------------------
# main USI loop
# ------------------------------------------------------------

def main() -> None:
    engine_cmd = os.environ.get("TASO_ENGINE_CMD", ENGINE_CMD_DEFAULT)

    current_position = "position startpos"
    pending_options: List[str] = []
    last_go_line = "go"

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue

        debug(f"IN: {line}")

        # ---------------- USI handshake ----------------

        if line == "usi":
            out("id name TASO_USI")
            out("id author taso")
            out("usiok")
            continue

        if line == "isready":
            out("readyok")
            continue

        # ---------------- state capture ----------------

        if line.startswith("setoption"):
            pending_options.append(line)
            continue

        if line.startswith("usinewgame"):
            pending_options.clear()
            continue

        if line.startswith("position "):
            current_position = line
            continue

        # ---------------- go command ----------------

        if line.startswith("go"):
            last_go_line = line

            if not os.path.isfile(engine_cmd):
                out(f"info string ERROR: engine not found: {engine_cmd}")
                out("bestmove resign")
                continue

            # --- build downstream input ---
            feed_lines: List[str] = [
                "usi",
                "isready",
                *pending_options,
                current_position,
                last_go_line,
                "quit",
            ]
            feed = "\n".join(feed_lines) + "\n"

            debug("RUN engine subprocess")

            try:
                proc = subprocess.Popen(
                    [engine_cmd],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                assert proc.stdin and proc.stdout

                proc.stdin.write(feed)
                proc.stdin.flush()
                proc.stdin.close()

                bestmove_line: Optional[str] = None
                humanscore_sent = False

                start = time.time()

                while True:
                    if time.time() - start > ENGINE_TIMEOUT_SEC:
                        debug("ENGINE TIMEOUT")
                        break

                    out_line = proc.stdout.readline()
                    if not out_line:
                        break

                    s = out_line.rstrip("\n")
                    if not s:
                        continue

                    # swallow duplicate handshake
                    if s.startswith(("id ", "usiok", "readyok")):
                        continue

                    if s.startswith("info "):
                        if "humanscore" in s:
                            humanscore_sent = True
                        out(s)
                        continue

                    if s.startswith("bestmove"):
                        bestmove_line = s
                        break

                if bestmove_line is None:
                    if not humanscore_sent:
                        out("info string humanscore=0.50")
                    out("bestmove resign")
                else:
                    if not humanscore_sent:
                        out("info string humanscore=0.50")
                    out(bestmove_line)

                # stderr is debug-only
                if DEBUG and proc.stderr:
                    err = proc.stderr.read()
                    if err:
                        debug("DOWNSTREAM STDERR:\n" + err)

                proc.wait(timeout=0.5)

            except Exception as e:
                out(f"info string ERROR: {type(e).__name__}: {e}")
                out("bestmove resign")

            finally:
                pending_options.clear()

            continue

        # ---------------- quit ----------------

        if line == "quit":
            break

        debug(f"IGNORED: {line}")


if __name__ == "__main__":
    main()
