cat > ~/shogi/wrapper/taso_engine_fukaura.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# TASO Engine — FUKAURAOU ONLY (Mac / 32GB SAFE / 嫌らしさ増し完成凍結版 + 2手一致トラップ)
#
#  - Engine: ふかうら王 1本
#  - HumanScore: estimateのみ
#  - MultiPV最大depthスナップショット
#  - 勝勢: 収束＋長prefix（人間が維持しやすい）
#  - 劣勢: 2手一致を最大悪意で裏切る（Two-ply convergence trap）
#  - BUNKER: spread大なら安定寄せ
#  - COMEBACK: 悪い＋HWS低 → 事故誘発寄り
#  - SUDDEN DEATH: さらに悪い → MultiPV抽選
#
# ★安定化パッチ込み：
#   (1) setoption はそのまま通し、強制注入は usinewgame/usi 後のみ
#   (2) position が来てない場合は cp 符号反転しない（TURN_SIGN保険）
# =========================================================

# --------------------------
# toggles / path
# --------------------------
TASO_SHOW="${TASO_SHOW:-1}"
FUKA_BIN="${FUKA_BIN:-/Users/taso/shogi/engines/fukauraou/fukauraou}"

say(){ [ "$TASO_SHOW" = "1" ] && echo "info string $*"; }
is_num(){ [[ "${1:-}" =~ ^-?[0-9]+$ ]]; }

estimate_hws(){
  awk -v cp="${1:-0}" 'BEGIN{
    x = cp / 450.0
    h = 1.0 / (1.0 + exp(-x))
    if (h<0.01) h=0.01
    if (h>0.99) h=0.99
    printf "%.2f\n", h
  }'
}

# --------------------------
# thresholds (cp adjusted to side-to-move)
# --------------------------
TASO_WIN_CP="${TASO_WIN_CP:-300}"
TASO_LOSE_CP="${TASO_LOSE_CP:--300}"
COMEBACK_CP="${COMEBACK_CP:--600}"

# SUDDEN DEATH
SUDDEN_DEATH_MODE=0
TASO_SD_SCORE_LIMIT="${TASO_SD_SCORE_LIMIT:--650}"
TASO_SD_MPV_MIN="${TASO_SD_MPV_MIN:-2}"
TASO_SD_MPV_MAX="${TASO_SD_MPV_MAX:-3}"

# engine safe defaults
TASO_MULTIPV="${TASO_MULTIPV:-3}"
TASO_THREADS="${TASO_THREADS:-8}"
TASO_HASH_MB="${TASO_HASH_MB:-1024}"

# BUNKER: spread = cp1 - cp2
BUNKER_SPREAD="${BUNKER_SPREAD:-300}"

# WIN: stable selector
TASO_STABLE_MPV_MIN="${TASO_STABLE_MPV_MIN:-1}"
TASO_STABLE_MPV_MAX="${TASO_STABLE_MPV_MAX:-3}"
TASO_STABLE_DROP="${TASO_STABLE_DROP:-80}"
TASO_PREFIX_K="${TASO_PREFIX_K:-4}"

# LOSE: annoying selector
TASO_ANNOY_MPV_MIN="${TASO_ANNOY_MPV_MIN:-2}"
TASO_ANNOY_MPV_MAX="${TASO_ANNOY_MPV_MAX:-3}"
TASO_ANNOY_MAX_DROP="${TASO_ANNOY_MAX_DROP:-180}"

# ---- evil knobs ----
TASO_EVIL_MODE="${TASO_EVIL_MODE:-1}"  # 0 => early-diverge random fallback
TASO_EVIL_FAKE_CONV_BONUS="${TASO_EVIL_FAKE_CONV_BONUS:-18}"
TASO_EVIL_EARLY_DIVERGE_W="${TASO_EVIL_EARLY_DIVERGE_W:-10}"
TASO_EVIL_MID_DROP_BONUS="${TASO_EVIL_MID_DROP_BONUS:-10}"
TASO_EVIL_MID_DROP_MIN="${TASO_EVIL_MID_DROP_MIN:-40}"
TASO_EVIL_MID_DROP_MAX="${TASO_EVIL_MID_DROP_MAX:-120}"

# ---- 2手一致（Two-ply convergence）専用 ----
TASO_EVIL_TWOPLY_BONUS="${TASO_EVIL_TWOPLY_BONUS:-40}"
TASO_EVIL_LONGPREFIX_PENALTY="${TASO_EVIL_LONGPREFIX_PENALTY:-12}"

# --------------------------
# startup checks
# --------------------------
if [ ! -x "$FUKA_BIN" ]; then
  echo "id name TASO (fukauraou missing)"
  echo "id author taso"
  echo "usiok"
  exit 0
fi

# --------------------------
# start engine
# --------------------------
coproc ENG { "$FUKA_BIN"; }
exec 3>&"${ENG[1]}" 4<"${ENG[0]}"
trap 'echo quit >&3 2>/dev/null || true' EXIT INT TERM

# --------------------------
# state
# --------------------------
TURN_SIGN=1
HAVE_POS=0
LAST_SCORE=0
LAST_MATE=""

declare -A PV_MOVE PV_CP PV_MATE PV_DEPTH PV_PVLINE

reset_pv(){
  PV_MOVE=(); PV_CP=(); PV_MATE=(); PV_DEPTH=(); PV_PVLINE=()
}

apply_forced_options(){
  echo "setoption name Threads value $TASO_THREADS" >&3 || true
  echo "setoption name Hash value $TASO_HASH_MB" >&3 || true
  echo "setoption name MultiPV value $TASO_MULTIPV" >&3 || true
}

bunker_flag(){
  local cp1="${1:-0}" cp2="${2:-0}"
  local sp=$((cp1 - cp2))
  if (( sp >= BUNKER_SPREAD )); then
    echo "1 $sp"
  else
    echo "0 $sp"
  fi
}

common_prefix_len(){
  local a="$1" b="$2" k="$3"
  awk -v A="$a" -v B="$b" -v K="$k" 'BEGIN{
    n=split(A,aa," "); m=split(B,bb," ");
    lim=K; if(n<lim) lim=n; if(m<lim) lim=m;
    c=0;
    for(i=1;i<=lim;i++){
      if(aa[i]==bb[i]) c++;
      else break;
    }
    print c;
  }'
}

pick_from_range_with_drop(){
  local min="$1" max="$2" cp1="$3" limit="$4"
  local choices=() i
  for i in $(seq "$min" "$max"); do
    local mv="${PV_MOVE[$i]:-}"
    local cp="${PV_CP[$i]:-}"
    [ -n "$mv" ] || continue
    is_num "$cp" || continue
    local drop=$(( cp1 - cp ))
    if (( drop <= limit )); then
      choices+=("$mv")
    fi
  done
  if [ "${#choices[@]}" -gt 0 ]; then
    echo "${choices[$((RANDOM % ${#choices[@]}))]}"
  else
    echo ""
  fi
}

emit_comeback_and_sd(){
  local cp1="$1" mate1="${2:-}" hws="$3"
  SUDDEN_DEATH_MODE=0
  [ -n "$mate1" ] && return 0

  if (( cp1 <= TASO_SD_SCORE_LIMIT )); then
    SUDDEN_DEATH_MODE=1
    say "☠🎯 SUDDEN DEATH: 崩れ筋狙い"
    return 0
  fi

  if (( cp1 <= COMEBACK_CP )); then
    awk -v h="$hws" 'BEGIN{exit(!(h<0.45))}' && say "☠🎭 COMEBACK: 事故誘発寄り" || say "☠🐍 COMEBACK: ジワ粘り"
  fi
}

# --------------------------
# WIN selector: robust + long prefix
# --------------------------
pick_stable_winning_move(){
  local cp1="${PV_CP[1]:-0}"
  local mv1="${PV_MOVE[1]:-}"
  local pv1="${PV_PVLINE[1]:-}"
  [ -n "$mv1" ] || { echo ""; return 0; }

  [ -n "${PV_MATE[1]:-}" ] && { echo "$mv1"; return 0; }

  local min="$TASO_STABLE_MPV_MIN"
  local max="$TASO_STABLE_MPV_MAX"

  declare -A VOTES BESTDROP BESTPREF
  local i
  for i in $(seq "$min" "$max"); do
    local mv="${PV_MOVE[$i]:-}"
    local cp="${PV_CP[$i]:-}"
    local pv="${PV_PVLINE[$i]:-}"
    [ -n "$mv" ] || continue
    is_num "$cp" || continue

    local drop=$(( cp1 - cp ))
    (( drop < 0 )) && drop=0
    (( drop <= TASO_STABLE_DROP )) || continue

    VOTES["$mv"]=$(( ${VOTES["$mv"]:-0} + 1 ))

    local bd="${BESTDROP["$mv"]:-999999}"
    (( drop < bd )) && BESTDROP["$mv"]="$drop"

    local pref=0
    if [ -n "$pv1" ] && [ -n "$pv" ]; then
      pref="$(common_prefix_len "$pv1" "$pv" "$TASO_PREFIX_K")"
    fi
    local bp="${BESTPREF["$mv"]:-0}"
    (( pref > bp )) && BESTPREF["$mv"]="$pref"
  done

  local best="$mv1"
  local best_votes=-1 best_drop=999999 best_pref=-1
  local mv
  for mv in "${!VOTES[@]}"; do
    local v="${VOTES["$mv"]}"
    local d="${BESTDROP["$mv"]:-999999}"
    local p="${BESTPREF["$mv"]:-0}"
    if (( v > best_votes )) || { (( v == best_votes )) && (( d < best_drop )); } || { (( v == best_votes )) && (( d == best_drop )) && (( p > best_pref )); }; then
      best="$mv"
      best_votes="$v"
      best_drop="$d"
      best_pref="$p"
    fi
  done

  echo "$best"
}

# --------------------------
# LOSE selector: evil scoring + 2手一致トラップ
# --------------------------
pick_annoying_losing_move(){
  local cp1="${PV_CP[1]:-0}"
  local mv1="${PV_MOVE[1]:-}"
  local pv1="${PV_PVLINE[1]:-}"
  [ -n "$mv1" ] || { echo ""; return 0; }

  [ -n "${PV_MATE[1]:-}" ] && { echo "$mv1"; return 0; }

  local min="$TASO_ANNOY_MPV_MIN"
  local max="$TASO_ANNOY_MPV_MAX"

  # fallback（evil off）
  if (( TASO_EVIL_MODE == 0 )); then
    local best_pref=999999
    local candidates=() i
    for i in $(seq "$min" "$max"); do
      local mv="${PV_MOVE[$i]:-}" cp="${PV_CP[$i]:-}" pv="${PV_PVLINE[$i]:-}"
      [ -n "$mv" ] || continue
      is_num "$cp" || continue
      local drop=$(( cp1 - cp ))
      (( drop <= TASO_ANNOY_MAX_DROP )) || continue
      local pref=0
      [ -n "$pv1" ] && [ -n "$pv" ] && pref="$(common_prefix_len "$pv1" "$pv" "$TASO_PREFIX_K")"
      if (( pref < best_pref )); then best_pref="$pref"; candidates=("$mv")
      elif (( pref == best_pref )); then candidates+=("$mv"); fi
    done
    [ "${#candidates[@]}" -gt 0 ] && echo "${candidates[$((RANDOM % ${#candidates[@]}))]}" || echo "$mv1"
    return 0
  fi

  local best_score=-999999
  local candidates=() i

  for i in $(seq "$min" "$max"); do
    local mv="${PV_MOVE[$i]:-}"
    local cp="${PV_CP[$i]:-}"
    local pv="${PV_PVLINE[$i]:-}"
    [ -n "$mv" ] || continue
    is_num "$cp" || continue

    local drop=$(( cp1 - cp ))
    (( drop <= TASO_ANNOY_MAX_DROP )) || continue

    local pref=0
    if [ -n "$pv1" ] && [ -n "$pv" ]; then
      pref="$(common_prefix_len "$pv1" "$pv" "$TASO_PREFIX_K")"
    fi

    local score=0

    # (A) 早分岐ほど嫌
    score=$(( score + (TASO_PREFIX_K - pref) * TASO_EVIL_EARLY_DIVERGE_W ))

    # (B) ★意図的悪意：2手一致を最大評価★
    if (( pref == 2 )); then
      score=$(( score + TASO_EVIL_TWOPLY_BONUS ))
    fi

    # (C) 初手一致もボーナス（擬似一本道）
    if (( pref == 1 )); then
      score=$(( score + TASO_EVIL_FAKE_CONV_BONUS ))
    fi

    # (D) 3手以上一致は警戒域なので減点
    if (( pref >= 3 )); then
      score=$(( score - TASO_EVIL_LONGPREFIX_PENALTY * (pref - 2) ))
    fi

    # (E) drop中途半端は「見た目保つが難しい」
    if (( drop >= TASO_EVIL_MID_DROP_MIN && drop <= TASO_EVIL_MID_DROP_MAX )); then
      score=$(( score + TASO_EVIL_MID_DROP_BONUS ))
    fi

    if (( score > best_score )); then
      best_score="$score"
      candidates=("$mv")
    elif (( score == best_score )); then
      candidates+=("$mv")
    fi
  done

  if [ "${#candidates[@]}" -gt 0 ]; then
    echo "${candidates[$((RANDOM % ${#candidates[@]}))]}"
  else
    echo "$mv1"
  fi
}

pick_override_bestmove(){
  local mv1="${PV_MOVE[1]:-}"
  [ -n "$mv1" ] || { echo ""; return 0; }

  local cp1="${PV_CP[1]:-0}"
  local cp2="${PV_CP[2]:-$cp1}"
  local mate1="${PV_MATE[1]:-}"

  [ -n "$mate1" ] && { echo "$mv1"; return 0; }

  read bunker spread < <(bunker_flag "$cp1" "$cp2")

  if (( SUDDEN_DEATH_MODE == 1 )); then
    local pick
    pick="$(pick_from_range_with_drop "$TASO_SD_MPV_MIN" "$TASO_SD_MPV_MAX" "$cp1" 999999)"
    [ -n "$pick" ] && { echo "$pick"; return 0; }
    echo "$mv1"; return 0
  fi

  if (( cp1 >= TASO_WIN_CP )); then
    echo "$(pick_stable_winning_move)"; return 0
  fi

  if (( bunker == 1 )); then
    echo "$(pick_stable_winning_move)"; return 0
  fi

  if (( cp1 <= TASO_LOSE_CP )); then
    echo "$(pick_annoying_losing_move)"; return 0
  fi

  echo "$mv1"
}

# =========================================================
# USI loop
# =========================================================
while IFS= read -r line; do

  if [[ "$line" == position* ]]; then
    HAVE_POS=1
    mc="$(echo "$line" | awk '{for(i=1;i<=NF;i++) if($i=="moves"){print NF-i; exit} print 0}')"
    (( mc % 2 == 0 )) && TURN_SIGN=1 || TURN_SIGN=-1
    echo "$line" >&3
    continue
  fi

  if [[ "$line" == usi* ]]; then
    echo "$line" >&3
    while IFS= read -r o <&4; do
      echo "$o"
      [[ "$o" == usiok* ]] && break
    done
    apply_forced_options
    continue
  fi

  if [[ "$line" == isready* ]]; then
    echo "$line" >&3
    while IFS= read -r o <&4; do
      [[ "$o" == readyok* ]] && break
    done
    echo readyok
    continue
  fi

  # ★安定化(1)：setoptionは通すだけ（強制注入はしない）
  if [[ "$line" == setoption* ]]; then
    echo "$line" >&3
    continue
  fi

  # ★安定化(1)：usinewgame時にだけ強制注入
  if [[ "$line" == usinewgame* ]]; then
    echo "$line" >&3
    apply_forced_options || true
    continue
  fi

  if [[ "$line" == go* ]]; then
    reset_pv
    echo "$line" >&3
    bestmove_line="bestmove resign"

    while IFS= read -r o <&4; do
      if [[ "$o" == info* ]]; then
        echo "$o"

        mpv="$(echo "$o" | awk '{for(i=1;i<=NF;i++) if($i=="multipv"){print $(i+1); exit}}')"
        [ -n "${mpv:-}" ] || mpv="1"

        depth="$(echo "$o" | awk '{for(i=1;i<=NF;i++) if($i=="depth"){print $(i+1); exit}}')"
        [[ "${depth:-}" =~ ^[0-9]+$ ]] || depth=0

        prev="${PV_DEPTH[$mpv]:-0}"
        (( depth >= prev )) || continue
        PV_DEPTH["$mpv"]="$depth"

        if [[ "$o" == *" pv "* ]]; then
          pvline="$(echo "$o" | sed 's/.* pv //')"
          PV_PVLINE["$mpv"]="$pvline"
          mv="$(echo "$pvline" | awk '{print $1}')"
          [ -n "$mv" ] && PV_MOVE["$mpv"]="$mv"
        fi

        # ★安定化(2)：position未到達なら符号反転しない
        if [[ "$o" == *"score cp"* ]]; then
          cp="$(echo "$o" | sed 's/.*score cp //' | awk '{print $1}')"
          if is_num "$cp"; then
            if (( HAVE_POS == 1 )); then
              PV_CP["$mpv"]=$(( cp * TURN_SIGN ))
            else
              PV_CP["$mpv"]="$cp"
            fi
          fi
        fi

        if [[ "$o" == *" mate "* ]]; then
          mt="$(echo "$o" | sed 's/.* mate //' | awk '{print $1}')"
          [ -n "$mt" ] && PV_MATE["$mpv"]="$mt"
        fi

        continue
      fi

      if [[ "$o" == bestmove* ]]; then
        bestmove_line="$o"
        break
      fi
    done

    cp1="${PV_CP[1]:-0}"
    cp2="${PV_CP[2]:-$cp1}"
    mate1="${PV_MATE[1]:-}"

    HWS="$(estimate_hws "$cp1")"
    say "🧠 人間勝率: $HWS (cp=$cp1)"

    emit_comeback_and_sd "$cp1" "$mate1" "$HWS"

    override="$(pick_override_bestmove)"
    if [ -n "$override" ]; then
      if (( SUDDEN_DEATH_MODE == 1 )); then
        say "☠🎯 SD: MultiPV抽選"
      elif (( cp1 >= TASO_WIN_CP )); then
        say "🛡 優勢: 収束＋prefix（人間向け安定勝ち）"
      elif (( cp1 <= TASO_LOSE_CP )); then
        say "🐍 劣勢: 2手一致トラップ（確信点で裏切る）"
      else
        read bunker spread < <(bunker_flag "$cp1" "$cp2")
        (( bunker == 1 )) && say "🏖 バンカー: 安定寄せ(spread=$spread)"
      fi
      echo "bestmove $override"
      LAST_SCORE="$cp1"
      LAST_MATE="$mate1"
    else
      echo "$bestmove_line"
    fi

    continue
  fi

  echo "$line" >&3
done
SH

chmod +x ~/shogi/wrapper/taso_engine_fukaura.sh
