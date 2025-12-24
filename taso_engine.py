#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import time
import math
import random
import re
from typing import Dict, Optional, Tuple, List

# =========================================================
# TASO Engine (Python) — FUKAURAOU ONLY / 完成凍結版
#
# 思想:
# - 勝勢: 人間が維持しやすい安定手
# - 劣勢: 2手一致トラップを含む「嫌らしい」逆転含み
# - 終盤: 演出停止・静的
# =========================================================

# --------------------------
# env toggles / paths
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
COMEBACK_CP = int(os.environ.get("COMEBACK_CP", "-600"))

BUNKER_SPREAD = int(os.environ.get("BUNKER_SPREAD", "300"))

TASO_SD_SCORE_LIMIT = int(os.environ.get("TASO_SD_SCORE_LIMIT", "-650"))
TASO_SD_MPV_MIN = int(os.environ.get("TASO_SD_MPV_MIN", "2"))
TASO_SD_MPV_MAX = int(os.environ.get("TASO_SD_MPV_MAX", "3"))

TASO_STABLE_MPV_MIN = int(os.environ.get("TASO_STABLE_MPV_MIN", "1"))
TASO_STABLE_MPV_MAX = int(os.environ.get("TASO_STABLE_MPV_MAX", "3"))
TASO_STABLE_DROP = int(os.environ.get("TASO_STABLE_DROP", "80"))
TASO_PREFIX_K = int(os.environ.get("TASO_PREFIX_K", "4"))

TASO_ANNOY_MPV_MIN = int(os.environ.get("TASO_ANNOY_MPV_MIN", "2"))
TASO_ANNOY_MPV_MAX = int(os.environ.get("TASO_ANNOY_MPV_MAX", "3"))
TASO_ANNOY_MAX_DROP = int(os.environ.get("TASO_ANNOY_MAX_DROP", "180"))

TASO_EVIL_MODE = int(os.environ.get("TASO_EVIL_MODE", "1"))
TASO_EVIL_FAKE_CONV_BONUS = int(os.environ.get("TASO_EVIL_FAKE_CONV_BONUS", "18"))
TASO_EVIL_EARLY_DIVERGE_W = int(os.environ.get("TASO_EVIL_EARLY_DIVERGE_W", "10"))
TASO_EVIL_MID_DROP_BONUS = int(os.environ.get("TASO_EVIL_MID_DROP_BONUS", "10"))
TASO_EVIL_MID_DROP_MIN = int(os.environ.get("TASO_EVIL_MID_DROP_MIN", "40"))
TASO_EVIL_MID_DROP_MAX = int(os.environ.get("TASO_EVIL_MID_DROP_MAX", "120"))

TASO_EVIL_TWOPLY_BONUS = int(os.environ.get("TASO_EVIL_TWOPLY_BONUS", "40"))
TASO_EVIL_LONGPREFIX_PENALTY = int(os.environ.get("TASO_EVIL_LONGPREFIX_PENALTY", "12"))

ENDGAME_MOVECOUNT = int(os.environ.get("TASO_ENDGAME_MOVECOUNT", "90"))
ABNORMAL_DROP = int(os.environ.get("TASO_ABNORMAL_DROP", "600"))

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

def estimate_hws(cp: int) -> float:
    x = cp / 450.0
    h = 1.0 / (1.0 + math.exp(-x))
    return max(0.01, min(0.99, h))

def common_prefix_len(a: str, b: str, k: int) -> int:
    aa = a.split()
    bb = b.split()
    lim = min(k, len(aa), len(bb))
    for i in range(lim):
        if aa[i] != bb[i]:
            return i
    return lim

# --------------------------
# Child engine
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
            self.proc.stdin.write(s + "\n")
            self.proc.stdin.flush()
        except Exception:
            pass

    def readline(self, timeout: float) -> Optional[str]:
        start = time.time()
        while time.time() - start < timeout:
            line = self.proc.stdout.readline()
            if line:
                return line.rstrip("\n")
            if self.proc.poll() is not None:
                return None
        return None

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
# PV Store
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
# selection helpers
# --------------------------
def bunker_flag(cp1: int, cp2: int) -> Tuple[int, int]:
    sp = cp1 - cp2
    return (1, sp) if sp >= BUNKER_SPREAD else (0, sp)

def pick_from_range(pvs: PVStore, a: int, b: int) -> Optional[str]:
    xs = [pvs.move[i] for i in range(a, b + 1) if i in pvs.move]
    return random.choice(xs) if xs else None

# --- 安定勝ち ---
def pick_stable_winning_move(pvs: PVStore) -> Optional[str]:
    if 1 in pvs.mate:
        return pvs.move.get(1)
    cp1 = pvs.cp.get(1, 0)
    pv1 = pvs.pvline.get(1, "")
    best = pvs.move.get(1)
    best_score = -1

    for i in range(TASO_STABLE_MPV_MIN, TASO_STABLE_MPV_MAX + 1):
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
            best = mv
            best_score = score
    return best

# --- 嫌らしさ ---
def pick_annoying_losing_move(pvs: PVStore) -> Optional[str]:
    if 1 in pvs.mate:
        return pvs.move.get(1)
    cp1 = pvs.cp.get(1, 0)
    pv1 = pvs.pvline.get(1, "")
    best_score = -10**9
    cand: List[str] = []

    for i in range(TASO_ANNOY_MPV_MIN, TASO_ANNOY_MPV_MAX + 1):
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
# main
# --------------------------
def main() -> None:
    if not os.path.isfile(FUKA_BIN):
        out("id name TASO (fukauraou missing)")
        out("id author taso")
        out("usiok")
        return

    eng = ChildEngine([FUKA_BIN])
    pvs = PVStore()
    have_pos = False
    turn_sign = 1
    move_count = 0
    last_score = 0

    def apply_opts(mpv: Optional[int] = None) -> None:
        eng.send(f"setoption name Threads value {TASO_THREADS}")
        eng.send(f"setoption name Hash value {TASO_HASH_MB}")
        eng.send(f"setoption name MultiPV value {mpv if mpv else TASO_MULTIPV}")

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
                    if not o:
                        break
                    if o.startswith("readyok"):
                        break
                out("readyok")
                continue

            if line.startswith("position"):
                have_pos = True
                toks = line.split()
                if "moves" in toks:
                    move_count = len(toks) - toks.index("moves") - 1
                turn_sign = 1 if move_count % 2 == 0 else -1
                eng.send(line)
                continue

            if line.startswith("go"):
                pvs.reset()
                endgame = move_count >= ENDGAME_MOVECOUNT
                apply_opts(1 if endgame else TASO_MULTIPV)
                eng.send(line)

                best_engine = None

                while True:
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
                                cp = int(toks[j+1]) * (turn_sign if have_pos else 1)
                        if "mate" in toks:
                            mate = toks[toks.index("mate")+1]
                        pvs.snapshot(mpv, depth, mv, pv, cp, mate)
                        continue
                    if o.startswith("bestmove"):
                        best_engine = o.split()[1]
                        break

                cp1 = pvs.cp.get(1, 0)
                cp2 = pvs.cp.get(2, cp1)
                hws = 0.5 if endgame else estimate_hws(cp1)
                out(f"info string humanscore={hws:.2f}")

                override = best_engine
                if not endgame:
                    b, _ = bunker_flag(cp1, cp2)
                    if cp1 >= TASO_WIN_CP or b:
                        override = pick_stable_winning_move(pvs)
                    elif cp1 <= TASO_LOSE_CP:
                        override = pick_annoying_losing_move(pvs)

                out(f"bestmove {override}")
                last_score = cp1
                apply_opts()
                continue

            if line == "quit":
                break

            eng.send(line)

    finally:
        eng.close()

if __name__ == "__main__":
    main()
