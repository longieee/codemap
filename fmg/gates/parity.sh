#!/usr/bin/env bash
# Parity harness: binary A (baseline) vs binary B (candidate) on one vault.
#
# Five outcomes, each named for the condition ACTUALLY checked. An earlier version
# tested only A's output length and then printed "(empty both sides)" — a condition it
# never checked — so a case where A emits nothing and B emits something was silently
# filed as NOT_EXERCISED. That is precisely the direction a capability-adding change
# moves, i.e. the harness was blind to its own subject. Emptiness is now evaluated on
# BOTH sides and the one-sided cases are reported as changes, never as no-evidence.
#
#   IDENTICAL          both substantive, byte-equal
#   DIFF               both substantive, not equal
#   CHANGED-A-EMPTY    A trivial, B substantive  -> a real change (capability added)
#   CHANGED-B-EMPTY    A substantive, B trivial  -> a real change (output lost)
#   NOT_EXERCISED      BOTH trivial              -> the only no-evidence case
#
# Usage: parity.sh <binary-A> <binary-B> <vault> <label>
set -u
A="$1"; B="$2"; VAULT="$3"; TAG="$4"

declare -A MARK=( [describe]="Vault Statistics" [xedges]="Cross-service" [orphans]="rphan" \
                  [broken]="roken" [centrality]="Most Connected" )

# "trivial" = we got nothing to compare: non-zero exit, near-empty output, or a JSON
# empty container. Returns 0 (true) when trivial.
is_trivial () {
  local out="$1" rc="$2" fmt="$3"
  [ "$rc" -ne 0 ] && return 0
  [ "${#out}" -lt 3 ] && return 0
  if [ "$fmt" = json ]; then
    case "$(printf '%s' "$out" | tr -d ' \n\r\t')" in "[]"|"{}"|"") return 0;; esac
  else
    [ "${#out}" -lt 10 ] && return 0
  fi
  return 1
}

ident=0; diff=0; chA=0; chB=0; vac=0
for cmd in describe xedges orphans broken centrality; do
  for fmt in text json; do
    a=$("$A" -w "$VAULT" "$cmd" --format "$fmt" 2>/dev/null); ra=$?
    b=$("$B" -w "$VAULT" "$cmd" --format "$fmt" 2>/dev/null); rb=$?
    ta=1; tb=1
    is_trivial "$a" "$ra" "$fmt" && ta=0
    is_trivial "$b" "$rb" "$fmt" && tb=0
    if [ $ta -eq 0 ] && [ $tb -eq 0 ]; then
      echo "$TAG $cmd/$fmt: NOT_EXERCISED (both sides trivial: A=${#a}B rc=$ra, B=${#b}B rc=$rb)"
      vac=$((vac+1)); continue
    fi
    if [ $ta -eq 0 ]; then
      echo "$TAG $cmd/$fmt: CHANGED-A-EMPTY (A trivial ${#a}B rc=$ra -> B substantive ${#b}B)"
      chA=$((chA+1)); continue
    fi
    if [ $tb -eq 0 ]; then
      echo "$TAG $cmd/$fmt: CHANGED-B-EMPTY (A substantive ${#a}B -> B trivial ${#b}B rc=$rb)"
      chB=$((chB+1)); continue
    fi
    # both substantive: require the subcommand's own marker (text) or JSON shape, so a
    # usage error is never compared against another usage error.
    bad=""
    if [ "$fmt" = text ]; then
      case "$a" in *"${MARK[$cmd]}"*) ;; *) bad="A_no_marker";; esac
      case "$b" in *"${MARK[$cmd]}"*) ;; *) bad="$bad B_no_marker";; esac
    else
      case "$a" in "{"*|"["*) ;; *) bad="A_not_json";; esac
      case "$b" in "{"*|"["*) ;; *) bad="$bad B_not_json";; esac
    fi
    if [ -n "$bad" ]; then
      echo "$TAG $cmd/$fmt: NOT_EXERCISED (output not recognisable:$bad)"; vac=$((vac+1)); continue
    fi
    if [ "$a" = "$b" ]; then echo "$TAG $cmd/$fmt: IDENTICAL (${#a}B)"; ident=$((ident+1))
    else echo "$TAG $cmd/$fmt: DIFF (A=${#a}B B=${#b}B)"; diff=$((diff+1)); fi
  done
done
echo "$TAG RESULT: identical=$ident diff=$diff changed_a_empty=$chA changed_b_empty=$chB not_exercised=$vac"
