#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import time
import math
import random
import re
from typing import Dict, Optional, List, Tuple

# =========================================================
# TASO Engine (Python) — FUKAURAOU ONLY / 配布用凍結版
#
# 修正履歴:
#  1) readline安全化（疑似タイムアウト事故防止）
#  2) Hash option 多重送信（NNUE互換性向上）
#  3) mate / 必至判断を info string で明示
# =========================================================

# --------------------------
# env / paths
# --------------------------
TASO_SHOW = os.environ.get("TASO_SHOW", "1") == "1"
TASO_DEBUG = os.environ.get("TASO_DEBUG", "0") == "1"

FUKA_BIN = os.environ.get(
    "FUKA_BIN",
    os.path.expanduser("~/shogi/engines/fukauraou/fukauraou")
)

TASO_THREADS = int(os.environ.get("TASO_THREADS", "8"))
TASO_HASH_MB = int(os.environ.get("TASO_HASH_MB", "1024"))
TASO_MULTIPV = int(os.environ.get("TASO_MULTIPV", "3"))

# thresholds
TASO_WIN_CP = int(os.environ.get("TASO_WIN_CP", "300"))
TASO_LOSE_CP = int(os.environ.get("TASO_LOSE_CP", "-300"))
ENDGAME_CP = int(os.environ.get("TASO_ENDGAME_CP", "800"))

# stable selector
TASO_STABLE_DROP = int(os.environ.get("TASO_STABLE_DROP", "120"))
TASO_PREFIX_K = int(os.environ.get("TASO_PREFIX_K", "4"))

# annoying selector
TASO_ANNOY_MAX_DROP = int(os.environ.get("TASO_ANNOY_MAX_DROP", "180"))

# evil knobs
TASO_EVIL_EARLY_DIVERGE_W = int(os.environ.get("TASO_EVIL_EARLY_DIVERGE_W", "10"))
TASO_EVIL_FAKE_CONV_BONUS = int(os.environ.get("TASO_EVIL_FAKE_CONV_BONUS", "18"))
TASO_EVIL_TWOPLY_BONUS = int(os.environ.get("TASO_EVIL_TWOPLY_BONUS", "40"))
TASO_EVIL_LONGPREFIX_PENALTY = int(os.environ.get("TASO_EVIL_LONGPREFIX_PENALTY", "12"))
TASO_EVIL_MID_DROP_BONUS = int(os.environ.get("TASO_EVIL_MID_DROP_BONUS", "10"))
TASO_EVIL_MID_DROP_MIN = int(os.environ.get("TASO_EVIL_MID_DROP_MIN", "40"))
TASO_EVIL_MID_DROP_MAX = int(os.environ.get("TASO_EVIL_MID_DROP_MAX", "120"))

# hisshi
TASO_HISSHI_PREFIX_MIN = int(os.environ.get("TASO_HISSHI_PREFIX_MIN", "2"))
TASO_HISSHI_MAX_DROP = int(os.environ.get("TASO_HISSHI_MAX_DROP", "120"))
TASO_HISSHI_MIN_LINES = int(os.environ.get("TASO_HISSHI_MIN_LINES", "2"))

# mate handling
TASO_SHORT_MATE_MAX = int(os.environ.get("TASO_SHORT_MATE_MAX", "7"))
TASO_LONG_MATE_IGNORE = int(os.environ.get("TASO_LONG_MATE_IGNORE", "10"))

READ_TIMEOUT_SEC = float(os.environ.get("TASO_READ_TIMEOUT", "2.0"))
LINE_LIMIT = int(os.environ.get("TASO_LINE_LIMIT", "6000"))

# --------------------------
# io helpers
# --------------------------
def out(s: str) -> None:
    sys.stdout.write(s + "\n")
    sys.stdout.flush()

def dbg(s: str) -> None:
    if TASO_DEBUG:
        sys.stderr.write("[TASO] " + s + "\n")
        sys.stderr.flush()

def say(s: str) -> None:
    if TASO_SHOW:
        out("info string " + s)

# --------------------------
# helpers
# --------------------------
_re_int = re.compile(r"^-?\d+$")

def is_int(s: str) -> bool:
    return bool(_re_int.match(s))

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def human_score(cp: int) -> float:
    x = cp / 450.0
    h = 1.0 / (1.0 + math.exp(-x))
    if abs(cp) >= ENDGAME_CP:
        h = 0.5 + (h - 0.5) * 0.6
    return clamp(h, 0.01, 0.99)

def common_prefix_len(a: str, b: str, k: int) -> int:
    aa = a.split()
    bb = b.split()
    lim = min(k, len(aa), len(bb))
    for i in range(lim):
        if aa[i] != bb[i]:
            return i
    return lim

def parse_mate(m: Optional[str]) -> Optional[int]:
    try:
        return int(m) if m is not None else None
    except Exception:
        return None

def turn_sign_from_position(line: str) -> int:
    toks = line.split()
    if "startpos" in toks and "moves" in toks:
        mc = len(toks) - toks.index("moves") - 1
        return 1 if mc % 2 == 0 else -1
    if "sfen" in toks:
        for t in toks:
            if t == "b":
                return 1
            if t == "w":
                return -1
    return 1

# --------------------------
# child engine (修正①)
# --------------------------
class ChildEngine:
    def __init__(self, cmd: List[str]) -> None:
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def send(self, s: str) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.write(s + "\n")
                self.proc.stdin.flush()
        except Exception:
            pass

    def readline(self, timeout: float) -> Optional[str]:
        if not self.proc.stdout:
            return None
        start = time.time()
        while True:
            if time.time() - start > timeout:
                return None
            if self.proc.poll() is not None:
                return None
            line = self.proc.stdout.readline()
            if line:
                return line.rstrip("\n")

    def close(self) -> None:
        try:
            self.send("quit")
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass

# --------------------------
# PV store
# --------------------------
class PVStore:
    def __init__(self) -> None:
        self.move: Dict[int, str] = {}
        self.cp: Dict[int, int] = {}
        self.mate: Dict[int, str] = {}
        self.depth: Dict[int, int] = {}
        self.pvline: Dict[int, str] = {}

    def reset(self) -> None:
        self.move.clear()
        self.cp.clear()
        self.mate.clear()
        self.depth.clear()
        self.pvline.clear()

    def snapshot(self, mpv: int, depth: int,
                 mv: Optional[str], pv: Optional[str],
                 cp: Optional[int], mate: Optional[str]) -> None:
        if depth < self.depth.get(mpv, -1):
            return
        self.depth[mpv] = depth
        if mv:
            self.move[mpv] = mv
        if pv:
            self.pvline[mpv] = pv
        if cp is not None:
            self.cp[mpv] = cp
        if mate:
            self.mate[mpv] = mate

# --------------------------
# 必至検出
# --------------------------
def is_hisshi_position(pvs: PVStore) -> bool:
    if 1 not in pvs.pvline or 1 not in pvs.cp:
        return False
    pv1 = pvs.pvline[1]
    cp1 = pvs.cp[1]
    hits = 0
    for i in range(2, TASO_MULTIPV + 1):
        pv = pvs.pvline.get(i)
        cp = pvs.cp.get(i)
        if not pv or cp is None:
            continue
        pref = common_prefix_len(pv1, pv, TASO_PREFIX_K)
        drop = abs(cp1 - cp)
        if pref >= TASO_HISSHI_PREFIX_MIN and drop <= TASO_HISSHI_MAX_DROP:
            hits += 1
    return hits >= TASO_HISSHI_MIN_LINES

# --------------------------
# move selection
# --------------------------
def pick_stable_winning_move(pvs: PVStore) -> Optional[str]:
    cp1 = pvs.cp.get(1, 0)
    pv1 = pvs.pvline.get(1, "")
    best = pvs.move.get(1)
    best_score = -10**9
    for i in range(1, TASO_MULTIPV + 1):
        mv = pvs.move.get(i)
        cp = pvs.cp.get(i)
        pv = pvs.pvline.get(i, "")
        if not mv or cp is None:
            continue
        drop = max(0, cp1 - cp)
        if drop > TASO_STABLE_DROP:
            continue
        pref = common_prefix_len(pv1, pv, TASO_PREFIX_K)
        score = -drop + pref * 5
        if score > best_score:
            best_score = score
            best = mv
    return best

def pick_annoying_losing_move(pvs: PVStore) -> Optional[str]:
    cp1 = pvs.cp.get(1, 0)
    pv1 = pvs.pvline.get(1, "")
    best_score = -10**9
    cand: List[str] = []
    for i in range(2, TASO_MULTIPV + 1):
        mv = pvs.move.get(i)
        cp = pvs.cp.get(i)
        pv = pvs.pvline.get(i, "")
        if not mv or cp is None:
            continue
        drop = cp1 - cp
        if drop > TASO_ANNOY_MAX_DROP:
            continue
        pref = common_prefix_len(pv1, pv, TASO_PREFIX_K)
        score = (TASO_PREFIX_K - pref) * TASO_EVIL_EARLY_DIVERGE_W
        if pref == 2:
            score += TASO_EVIL_TWOPLY_BONUS
        if pref == 1:
            score += TASO_EVIL_FAKE_CONV_BONUS
        if pref >= 3:
            score -= TASO_EVIL_LONGPREFIX_PENALTY * (pref - 2)
        if TASO_EVIL_MID_DROP_MIN <= drop <= TASO_EVIL_MID_DROP_MAX:
            score += TASO_EVIL_MID_DROP_BONUS
        if score > best_score:
            best_score = score
            cand = [mv]
        elif score == best_score:
            cand.append(mv)
    return random.choice(cand) if cand else pvs.move.get(1)

# --------------------------
# main loop
# --------------------------
def main() -> None:
    if not os.path.isfile(FUKA_BIN):
        out("id name TASO (fukauraou missing)")
        out("id author taso")
        out("usiok")
        return

    eng = ChildEngine([FUKA_BIN])
    pvs = PVStore()
    turn_sign = 1

    # 修正②: Hash option 多重送信
    def apply_opts() -> None:
        eng.send(f"setoption name Threads value {TASO_THREADS}")
        eng.send(f"setoption name USI_Hash value {TASO_HASH_MB}")
        eng.send(f"setoption name Hash value {TASO_HASH_MB}")
        eng.send(f"setoption name HashSize value {TASO_HASH_MB}")
        eng.send(f"setoption name MultiPV value {TASO_MULTIPV}")

    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue

            if line == "usi":
                eng.send("usi")
                while True:
                    o = eng.readline(READ_TIMEOUT_SEC)
                    if not o:
                        break
                    out(o)
                    if o.startswith("usiok"):
                        break
                apply_opts()
                continue

            if line == "isready":
                eng.send("isready")
                while True:
                    o = eng.readline(READ_TIMEOUT_SEC)
                    if o and o.startswith("readyok"):
                        break
                out("readyok")
                continue

            if line.startswith("position"):
                turn_sign = turn_sign_from_position(line)
                eng.send(line)
                continue

            if line.startswith("go"):
                pvs.reset()
                eng.send(line)
                best_engine: Optional[str] = None

                seen = 0
                while True:
                    seen += 1
                    if seen > LINE_LIMIT:
                        break
                    o = eng.readline(READ_TIMEOUT_SEC)
                    if not o:
                        break

                    if o.startswith("info"):
                        out(o)
                        toks = o.split()

                        mpv = int(toks[toks.index("multipv")+1]) if "multipv" in toks else 1
                        depth = int(toks[toks.index("depth")+1]) if "depth" in toks else 0

                        cp = None
                        mate = None
                        pv = None
                        mv = None

                        if "pv" in toks:
                            i = toks.index("pv")
                            pv = " ".join(toks[i+1:])
                            mv = toks[i+1]

                        if "score" in toks and "cp" in toks:
                            j = toks.index("cp")
                            if is_int(toks[j+1]):
                                cp = int(toks[j+1]) * turn_sign

                        if "mate" in toks:
                            mate = toks[toks.index("mate")+1]

                        pvs.snapshot(mpv, depth, mv, pv, cp, mate)
                        continue

                    if o.startswith("bestmove"):
                        best_engine = o.split()[1]
                        break

                cp1 = pvs.cp.get(1, 0)
                out(f"info string humanscore={human_score(cp1):.2f}")

                override = best_engine or "resign"

                # --- 修正③: 意思決定の可視化 ---
                best_short: Optional[Tuple[int, str]] = None
                for i, m in pvs.mate.items():
                    mt = parse_mate(m)
                    mv = pvs.move.get(i)
                    if mt and mv and (best_short is None or mt < best_short[0]):
                        best_short = (mt, mv)

                if best_short and best_short[0] <= TASO_SHORT_MATE_MAX:
                    say(f"⚡ 短手数詰み優先 (mate {best_short[0]})")
                    override = best_short[1]
                else:
                    has_long_mate = any(
                        parse_mate(m) and parse_mate(m) > TASO_LONG_MATE_IGNORE
                        for m in pvs.mate.values()
                    )
                    if is_hisshi_position(pvs) and not has_long_mate:
                        say("🔒 必至優先（長い詰みは保留）")
                        override = pick_stable_winning_move(pvs) or override
                    elif cp1 >= TASO_WIN_CP:
                        override = pick_stable_winning_move(pvs) or override
                    elif cp1 <= TASO_LOSE_CP:
                        override = pick_annoying_losing_move(pvs) or override

                out(f"bestmove {override}")
                apply_opts()
                continue

            if line == "quit":
                break

            eng.send(line)

    finally:
        eng.close()

if __name__ == "__main__":
    main()
