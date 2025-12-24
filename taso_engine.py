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
# TASO Engine (Python) — FUKAURAOU ONLY / 最新凍結版 + 必至優先
#
# 思想:
# - 勝勢: 人間が維持しやすい安定手（prefix長め・drop小）
# - 劣勢: 2手一致トラップを含む嫌らしい逆転含み
# - 終盤: 長い詰み < 短い詰み < 必至
# - 棋譜途中解析対応: position sfen でも手番判定が壊れない
# - 安全脱出: LINE_LIMIT + 読み取りタイムアウト
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

# --------------------------
# thresholds
# --------------------------
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

# hisshi (必至) detection
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
    if m is None:
        return None
    try:
        return int(m)
    except Exception:
        return None

def turn_sign_from_position(line: str) -> int:
    """
    エンジン出力の cp を「先手視点」に寄せるための符号。
    - position startpos moves ... : 手数パリティで先手/後手を推定
    - position sfen ...           : sfen の手番(b/w)から推定
    """
    toks = line.split()
    if len(toks) < 2:
        return 1

    # position startpos ...
    if len(toks) >= 2 and toks[1] == "startpos":
        if "moves" in toks:
            mc = len(toks) - toks.index("moves") - 1
            return 1 if (mc % 2 == 0) else -1
        return 1

    # position sfen <sfen...> <turn> <hand> <ply> [moves ...]
    if len(toks) >= 3 and toks[1] == "sfen":
        # sfenは固定4フィールド: board / turn / hand / ply
        # 例: position sfen ... b - 42
        # turn は b or w
        try:
            # board は可変だが、USIのposition sfenは「sfen の4フィールドを空白で区切って渡す」
            # よって turn は toks[?] のうち b/w の最初の出現を拾う
            for t in toks[2:]:
                if t in ("b", "w"):
                    return 1 if t == "b" else -1
        except Exception:
            pass
        return 1

    return 1

# --------------------------
# child engine
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
        # NOTE: stdout.readline() is blocking; timeout is "soft".
        start = time.time()
        while time.time() - start < timeout:
            if self.proc.stdout:
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
# 必至検出（簡易）
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

    def apply_opts() -> None:
        eng.send(f"setoption name Threads value {TASO_THREADS}")
        eng.send(f"setoption name Hash value {TASO_HASH_MB}")
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
                        dbg("LINE_LIMIT reached")
                        break

                    o = eng.readline(READ_TIMEOUT_SEC)
                    if not o:
                        dbg("engine silence/timeout")
                        break

                    if o.startswith("info"):
                        out(o)
                        toks = o.split()

                        mpv = 1
                        if "multipv" in toks:
                            try:
                                mpv = int(toks[toks.index("multipv") + 1])
                            except Exception:
                                mpv = 1

                        depth = 0
                        if "depth" in toks:
                            try:
                                depth = int(toks[toks.index("depth") + 1])
                            except Exception:
                                depth = 0

                        cp = None
                        mate = None
                        pv = None
                        mv = None

                        if "pv" in toks:
                            i = toks.index("pv")
                            if i + 1 < len(toks):
                                pv = " ".join(toks[i + 1 :])
                                mv = toks[i + 1]

                        if "score" in toks and "cp" in toks:
                            j = toks.index("cp")
                            if j + 1 < len(toks) and is_int(toks[j + 1]):
                                cp = int(toks[j + 1]) * turn_sign

                        if "mate" in toks:
                            k = toks.index("mate")
                            if k + 1 < len(toks):
                                mate = toks[k + 1]

                        pvs.snapshot(mpv, depth, mv, pv, cp, mate)
                        continue

                    if o.startswith("bestmove"):
                        parts = o.split()
                        if len(parts) >= 2:
                            best_engine = parts[1]
                        break

                cp1 = pvs.cp.get(1, 0)
                hws = human_score(cp1)
                out(f"info string humanscore={hws:.2f}")

                override = best_engine or "resign"

                # ---- 終盤ロジック：長い詰み < 短い詰み < 必至 ----
                endgame = abs(cp1) >= ENDGAME_CP

                # 1) 短い詰み（<= TASO_SHORT_MATE_MAX）を最優先
                best_short: Optional[Tuple[int, str]] = None
                for i, m in pvs.mate.items():
                    mt = parse_mate(m)
                    if mt is None or mt <= 0:
                        continue
                    mv = pvs.move.get(i)
                    if not mv:
                        continue
                    if best_short is None or mt < best_short[0]:
                        best_short = (mt, mv)

                if best_short is not None and best_short[0] <= TASO_SHORT_MATE_MAX:
                    override = best_short[1]

                else:
                    # 2) 長い詰み（>TASO_LONG_MATE_IGNORE）は「必至優先」の邪魔になるので無視
                    #    （mateが出ていても mt>TASO_LONG_MATE_IGNORE なら通常ロジックへ流す）
                    has_long_mate = False
                    for m in pvs.mate.values():
                        mt = parse_mate(m)
                        if mt is not None and mt > TASO_LONG_MATE_IGNORE:
                            has_long_mate = True
                            break

                    # 3) 必至っぽい局面なら必至優先（安定側へ）
                    if is_hisshi_position(pvs) and not has_long_mate:
                        say("🔒 必至優先")
                        override = pick_stable_winning_move(pvs) or override
                    else:
                        # 4) 通常の勝勢/劣勢思想
                        if cp1 >= TASO_WIN_CP:
                            override = pick_stable_winning_move(pvs) or override
                        elif cp1 <= TASO_LOSE_CP and not endgame:
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
