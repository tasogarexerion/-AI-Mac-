#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import random
import json
import time
from typing import Dict, List, Tuple, Optional

import shogi

# =============================
# Paths (dlshogi)
# =============================
DL = os.path.expanduser("~/shogi/engines/dlshogi/build/dlshogi")
DL_MODEL = os.path.expanduser("~/shogi/models/dlshogi_model/model.onnx")

OUT_DIR = os.path.expanduser("~/shogi/data/bunker_positions")
os.makedirs(OUT_DIR, exist_ok=True)

# =============================
# Args / Env
# =============================
GAMES = int(sys.argv[1]) if len(sys.argv) > 1 else 50

MOVETIME = int(os.environ.get("TASO_GEN_MOVETIME_MS", "40"))
PREFLIGHT = int(os.environ.get("TASO_GEN_PREFLIGHT_MS", "30"))
MULTIPV = int(os.environ.get("TASO_GEN_MULTIPV", "5"))
TOPK = int(os.environ.get("TASO_GEN_TOPK", "2"))
MAX_PLIES = int(os.environ.get("TASO_GEN_MAX_PLIES", "240"))

BUNKER_SPREAD = int(os.environ.get("TASO_BUNKER_SPREAD", "300"))
FOLLOW_PLIES = int(os.environ.get("TASO_FOLLOW_PLIES", "6"))

# Fear (quality) params
SAFE_GAP_CP = int(os.environ.get("TASO_FEAR_SAFE_GAP_CP", "120"))
SP_CLIP = float(os.environ.get("TASO_SP_CLIP", "800.0"))

# Random seed
SEED = os.environ.get("TASO_SEED")
if SEED is not None:
    random.seed(int(SEED))

# =============================
# ### PATCH: run id (prevent overwrite)
# =============================
RUN_ID = time.strftime("%Y%m%d_%H%M%S")

# =============================
# Utils
# =============================
def clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))

def normalize_cp(cp: float, board: shogi.Board) -> float:
    try:
        return float(cp) if board.turn == shogi.BLACK else -float(cp)
    except Exception:
        return float(cp)

# =============================
# Fear components
# =============================
def pv_branching(pvs: List[List[str]]) -> float:
    if len(pvs) < 2:
        return 0.0
    min_len = min(len(p) for p in pvs if p)
    if min_len <= 0:
        return 0.0
    same = 0
    for i in range(min_len):
        token = pvs[0][i]
        if all(len(p) > i and p[i] == token for p in pvs):
            same += 1
        else:
            break
    return clip01(1.0 - same / max(1, min_len))

def safe_move_loss_by_gap(norm_cps: List[float], best_cp: float, gap_cp: int = SAFE_GAP_CP) -> float:
    if not norm_cps:
        return 0.0
    safe = sum(1 for cp in norm_cps if (best_cp - cp) <= float(gap_cp))
    return clip01(1.0 - safe / max(1, len(norm_cps)))

def fear_teacher(pv_branch: float, safe_loss: float) -> float:
    return clip01(0.65 * pv_branch + 0.35 * safe_loss)

# =============================
# Pressure / Collapse
# =============================
def pressure_teacher(spread: float, legal_moves: int) -> Tuple[float, float, float]:
    bunker = clip01(abs(spread) / SP_CLIP)
    low_escape = clip01((25 - max(1, legal_moves)) / 20.0)
    return clip01(0.6 * bunker + 0.4 * low_escape), bunker, low_escape

def collapse_teacher(cp_before: float, cp_after: float, fear: float) -> float:
    flip = 1.0 if (cp_before > 300.0 and cp_after < 100.0) else 0.0
    return clip01(0.7 * flip + 0.3 * float(fear))

# =============================
# Engine I/O
# =============================
def start_engine() -> Tuple[subprocess.Popen, callable]:
    if not (os.path.exists(DL) and os.path.exists(DL_MODEL)):
        raise RuntimeError("dlshogi or model.onnx missing")

    p = subprocess.Popen(
        [DL, "--model", DL_MODEL, "--device", "metal",
         "--threads", "8", "--multipv", str(MULTIPV)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    def send(cmd: str) -> None:
        p.stdin.write(cmd + "\n")
        p.stdin.flush()

    def read_until(prefix: str, timeout_sec: float = 5.0) -> None:
        t0 = time.time()
        while True:
            if time.time() - t0 > timeout_sec:
                raise RuntimeError("engine timeout")
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("engine died")
            if line.strip().startswith(prefix):
                return

    send("usi")
    read_until("usiok")
    send("isready")
    read_until("readyok")
    return p, send

def parse_info_line(line: str) -> Optional[Tuple[int, int, List[str]]]:
    if not (line.startswith("info") and " pv " in line and "score cp" in line):
        return None
    parts = line.split()
    try:
        mpv = int(parts[parts.index("multipv") + 1]) if "multipv" in parts else 1
        cp = int(parts[parts.index("cp") + 1])
        pv = parts[parts.index("pv") + 1:]
        return mpv, cp, pv
    except Exception:
        return None

# =============================
# ### PATCH: timeout-safe multipv
# =============================
def go_collect_multipv(send, proc, pos_cmd: str, movetime_ms: int,
                        timeout_sec: float = 2.0) -> Tuple[Dict[int, int], Dict[int, List[str]]]:
    send(pos_cmd)
    send(f"go movetime {movetime_ms}")

    cps: Dict[int, int] = {}
    pvs: Dict[int, List[str]] = {}

    t0 = time.time()
    while True:
        if time.time() - t0 > timeout_sec:
            break
        line = proc.stdout.readline()
        if not line:
            break
        s = line.strip()
        if s.startswith("info"):
            parsed = parse_info_line(s)
            if parsed:
                mpv, cp, pv = parsed
                cps[mpv] = cp
                pvs[mpv] = pv
        if s.startswith("bestmove"):
            break
    return cps, pvs

def preflight(send, proc, pos_cmd: str):
    cps, pvs = go_collect_multipv(send, proc, pos_cmd, PREFLIGHT)
    if 1 not in cps or 2 not in cps:
        return None
    return cps, pvs

def pick_move_from_multipv(send, proc, pos_cmd: str, board: shogi.Board):
    cps, pvs = go_collect_multipv(send, proc, pos_cmd, MOVETIME)
    if not pvs:
        return None

    ordered = []
    for k in sorted(pvs.keys()):
        if pvs[k]:
            ordered.append((k, pvs[k][0], cps.get(k)))

    if not ordered:
        return None

    k = min(TOPK, len(ordered))
    chosen_rank, chosen_mv, chosen_cp_raw = ordered[random.randint(0, k - 1)]
    best_rank, best_mv, best_cp_raw = ordered[0]

    best_cp = normalize_cp(best_cp_raw or 0, board)
    chosen_cp = normalize_cp(chosen_cp_raw or 0, board)

    return {
        "move": chosen_mv,
        "chosen_rank": chosen_rank,
        "best_rank": best_rank,
        "bestmove": best_mv,
        "best_cp": best_cp,
        "chosen_cp": chosen_cp,
        "gap": best_cp - chosen_cp,
        "available_mpv": len(ordered),
    }

# =============================
# Main
# =============================
def main():
    proc, send = start_engine()
    saved_total = 0

    try:
        for g in range(GAMES):
            board = shogi.Board()
            moves: List[str] = []
            candidates: List[dict] = []

            for ply in range(MAX_PLIES):
                if board.is_game_over():
                    break

                pos = "position startpos moves " + " ".join(moves) if moves else "position startpos"
                legal_moves = len(list(board.legal_moves))

                pf = preflight(send, proc, pos)
                if pf:
                    cps_raw, pvs = pf
                    cp1 = normalize_cp(cps_raw[1], board)
                    cp2 = normalize_cp(cps_raw[2], board)
                    spread = cp1 - cp2

                    pv_list = [pvs[k] for k in sorted(pvs.keys()) if pvs.get(k)]
                    pv_branch = pv_branching(pv_list)

                    norm_cps = [normalize_cp(cp, board) for mpv, cp in sorted(cps_raw.items())]
                    safe_loss = safe_move_loss_by_gap(norm_cps, cp1)

                    fear = fear_teacher(pv_branch, safe_loss)

                    if spread >= BUNKER_SPREAD:
                        candidates.append({
                            "game": g,
                            "ply": ply,
                            "position": "startpos moves " + " ".join(moves),
                            "sfen": board.sfen(),
                            "cp1": cp1,
                            "cp2": cp2,
                            "spread": spread,
                            "fear": fear,
                            "pv_branch": pv_branch,
                            "safe_loss": safe_loss,
                            "safe_gap_cp": SAFE_GAP_CP,
                            "cp_path": [cp1],
                            "legal_moves": legal_moves,
                            "chosen_rank": None,
                            "bestmove": None,
                            "best_cp": None,
                            "chosen_cp": None,
                            "gap": None,
                        })

                pick = pick_move_from_multipv(send, proc, pos, board)
                if not pick:
                    break

                mv = pick["move"]
                board.push_usi(mv)
                moves.append(mv)

                for c in candidates:
                    if c["ply"] == ply:
                        c.update(pick)

                pf2 = preflight(send, proc, "position startpos moves " + " ".join(moves))
                if pf2:
                    cp_now = normalize_cp(pf2[0][1], board)
                    for c in candidates:
                        c["cp_path"].append(cp_now)

                ### PATCH: finalize only if cp_path complete
                done = []
                for c in candidates:
                    if len(c["cp_path"]) >= FOLLOW_PLIES + 1:
                        cp_after = c["cp_path"][-1]
                        pressure, bunker, low_escape = pressure_teacher(c["spread"], c["legal_moves"])
                        collapse = collapse_teacher(c["cp1"], cp_after, c["fear"])

                        out = dict(c)
                        out.update({
                            "follow_plies": FOLLOW_PLIES,
                            "cp_after": cp_after,
                            "pressure": pressure,
                            "bunker": bunker,
                            "low_escape": low_escape,
                            "collapse": collapse,
                        })

                        fn = f"bunker_{RUN_ID}_{g}_{c['ply']}.json"
                        with open(os.path.join(OUT_DIR, fn), "w", encoding="utf-8") as f:
                            json.dump(out, f, ensure_ascii=False, indent=2)

                        saved_total += 1
                        done.append(c)

                for c in done:
                    candidates.remove(c)

            print(f"[game {g+1}/{GAMES}] saved_total={saved_total}")

    finally:
        try:
            send("quit")
        except Exception:
            pass
        proc.terminate()

    print("DONE. saved_total=", saved_total)


if __name__ == "__main__":
    main()
