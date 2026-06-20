# shellcheck shell=bash
# ==============================================================================
# sandbox-discovery.sh — optional permission-discovery engine for sandbox-run
# ==============================================================================
# Sourced by `sandbox-run` ONLY in --discover mode. Runs the EXACT same sandbox
# under host-side strace (following forks), then classifies the paths the app
# could not access into actionable CATEGORIES (see PLAN.md "Phase 6").
#
# Engine guarantees these are defined when sourced: extract_bound_paths(),
# TARGET_BIN, EFFECTIVE_DEPS, SANDBOX_CONFIG, SANDBOX_STATE_DIR, HERE, PATH.
# ==============================================================================

# Errno failures treated as candidates. EROFS is included so write attempts that
# fail because the root is a tmpfs (the usual crash cause) are captured (R1).
DISCOVERY_ERRNOS="${DISCOVERY_ERRNOS:-ENOENT EACCES EROFS}"

run_discovery() {
  if ! command -v strace >/dev/null 2>&1; then
    echo "Error: --discover requires 'strace' (not found on PATH)." >&2
    return 1
  fi

  local ts run_dir trace failures probing summary bound rc=0
  ts="$(date +%Y%m%d-%H%M%S)"
  run_dir="$SANDBOX_STATE_DIR/discovery/${TARGET_BIN}-${ts}"
  mkdir -p "$run_dir"
  chmod 700 "$SANDBOX_STATE_DIR" 2>/dev/null || true
  trace="$run_dir/trace.raw"
  failures="$run_dir/failures.log"   # classified rows: bucket<TAB>count<TAB>lastnr<TAB>path
  probing="$run_dir/probing.log"
  summary="$run_dir/summary.txt"
  bound="$run_dir/bound_paths.txt"

  extract_bound_paths "$@" | sort -u > "$bound"

  echo
  echo "[discovery] Tracing '$TARGET_BIN'. Use it normally, then exit to analyze."
  echo "[discovery] Artifacts: $run_dir"
  echo

  strace -f -e trace=%file -o "$trace" bwrap "$@" || rc=$?

  : > "$failures"
  : > "$probing"
  _discovery_classify "$trace" "$bound" "$failures" "$probing"

  _discovery_summary "$failures" "$probing" > "$summary"

  echo
  if [ "$rc" -ne 0 ]; then
    echo "[discovery] '$TARGET_BIN' exited with code $rc."
  else
    echo "[discovery] '$TARGET_BIN' exited cleanly."
  fi
  echo "===================================================================="
  cat "$summary"
  echo "===================================================================="
  echo "[discovery] Full artifacts kept in: $run_dir"

  if grep -q $'^APP\t\|^WRITE\t' "$failures" 2>/dev/null; then
    local ans=""
    printf "[discovery] Show paste-ready bind suggestions (WRITE+APP)? [y/N] "
    { read -r ans </dev/tty; } 2>/dev/null || ans=""
    echo
    case "$ans" in
      y|Y) _discovery_suggest "$failures" ;;
    esac
  fi
  return "$rc"
}

# ------------------------------------------------------------------------------
# Single-pass awk classifier. Buckets each candidate path (failed, not already
# provided, never succeeded unless write-blocked) into exactly one of:
#   WRITE  app tried to create/write (EROFS/EACCES on a write syscall)   [R1]
#   PATHL  parent dir is a $PATH entry -> binary lookup                  [R2]
#   SYS    known system/libc config (/etc/...)                          [R3]
#   WALK   same basename failing across >=2 ancestor dirs of $HERE       [R4]
#   APP    everything else (the real signal)
# Also records last-seen order per path and the first fatal-exit position for
# the "most likely culprits" view. Probing (failed-then-succeeded) -> PROB.
# ------------------------------------------------------------------------------
_discovery_classify() {
  local trace="$1" bound="$2" failures="$3" probing="$4"
  awk -v errnos="$DISCOVERY_ERRNOS" -v FAIL="$failures" -v PROB="$probing" \
      -v here="$HERE" -v pathenv="$PATH" '
    BEGIN {
      ne = split(errnos, ea, " "); for (i=1;i<=ne;i++) ERR[ea[i]]=1
      # PATH entries -> set (normalized: collapse //, strip trailing /)
      np = split(pathenv, pa, ":")
      for (i=1;i<=np;i++){ d=pa[i]; gsub(/\/+/,"/",d); if(length(d)>1) sub(/\/$/,"",d); if(d!="") PSET[d]=1 }
      # known system/libc config prefixes
      ns = split("/etc/localtime /etc/passwd /etc/group /etc/shadow /etc/nsswitch.conf /etc/host.conf /etc/hosts /etc/resolv.conf /etc/netsvc.conf /etc/ld.so.preload /etc/ld.so.cache /etc/machine-id /etc/os-release /etc/lsb-release /etc/timezone /etc/openssl /etc/ssl /etc/pki /etc/gtk-2.0 /etc/gtk-3.0 /etc/fonts", sa, " ")
      for (i=1;i<=ns;i++) SYSP[sa[i]]=1
      fatal=0
    }
    FNR==NR { if($0!="") BOUND[$0]=1; next }
    {
      tnr++
      line=$0; id="-"
      if (match(line, /^[0-9]+ +/)) { id=substr(line,1,RLENGTH); gsub(/ /,"",id); sub(/^[0-9]+ +/,"",line) }

      # first fatal exit / crash signal position (SIGCHLD is normal, ignore it)
      if (fatal==0 && (line ~ /\+\+\+ exited with [1-9]/ || line ~ /--- SIG(SEGV|ABRT|BUS|KILL|FPE|ILL) /)) fatal=tnr

      if (line ~ /<unfinished \.\.\.>/) { p=getpath(line); if(p!=""){PENDP[id]=p; PENDW[id]=iswrite(line)} next }
      if (line ~ /<\.\.\. [a-z0-9_]+ resumed>/) { p=PENDP[id]; w=PENDW[id]; delete PENDP[id]; delete PENDW[id] }
      else { p=getpath(line); w=iswrite(line) }

      if (p=="" || substr(p,1,1)!="/") next
      gsub(/\/+/,"/",p)

      if (isfail(line)) { FAILC[p]++; LAST[p]=tnr; if(w) WR[p]=1 }
      else if (issucc(line)) SUCC[p]++
    }

    function getpath(s){ if (match(s, /"\/[^"]*"/)) return substr(s,RSTART+1,RLENGTH-2); return "" }
    function isfail(s,   e){ if (match(s, /= -1 [A-Z]+/)){ e=substr(s,RSTART+5,RLENGTH-5); return (e in ERR) } return 0 }
    function issucc(s){ if (s ~ /= -1 /) return 0; if (s ~ /= [0-9]/) return 1; if (s ~ /= 0x/) return 1; return 0 }
    function iswrite(s){
      if (s ~ /(mkdir|mkdirat|creat|rmdir|unlink|unlinkat|rename|renameat|renameat2|link|linkat|symlink|symlinkat|truncate|mknod|mknodat|chmod|fchmodat|chown|lchown|fchownat)\(/) return 1
      if (s ~ /open(at)?\(/ && s ~ /O_CREAT|O_WRONLY|O_RDWR/) return 1
      return 0
    }
    function is_bound(p,   n,parts,i,acc){ if(p in BOUND) return 1; n=split(p,parts,"/"); acc=""; for(i=2;i<=n;i++){acc=acc"/"parts[i]; if(acc in BOUND) return 1} return 0 }
    function parent(p,   k){ k=p; sub(/\/[^/]*$/,"",k); if(k=="")k="/"; return k }
    function base(p,   k){ k=p; sub(/.*\//,"",k); return k }
    function is_ancestor(par){ return (par==here || index(here"/", par"/")==1) }

    END {
      # pass 1: gather candidates + prelim bucket; collect ancestor-walk stats
      for (p in FAILC) {
        if (SUCC[p]>0 && !(p in WR)) { printf "%s\t%d\t%d\n", p, FAILC[p], SUCC[p] > PROB; continue }
        if (is_bound(p)) continue
        CAND[p]=1
        if (p in WR) B[p]="WRITE"
        else if (parent(p) in PSET) B[p]="PATHL"
        else if (is_sys(p)) B[p]="SYS"
        else { B[p]="?"; if (is_ancestor(parent(p))) { key=base(p); if(!(key SUBSEP parent(p) in SEEN)){SEEN[key SUBSEP parent(p)]=1; BNC[key]++} } }
      }
      # pass 2: finalize WALK vs APP, emit
      for (p in CAND) {
        b=B[p]
        if (b=="?") b = (BNC[base(p)]>=2 && is_ancestor(parent(p))) ? "WALK" : "APP"
        printf "%s\t%d\t%d\t%s\n", b, FAILC[p], LAST[p], p > FAIL
      }
      printf "META\tfatal\t%d\t-\n", fatal > FAIL
    }
    function is_sys(p,   n,parts,i,acc){ if(p in SYSP) return 1; n=split(p,parts,"/"); acc=""; for(i=2;i<=n;i++){acc=acc"/"parts[i]; if(acc in SYSP) return 1} return 0 }
  ' "$bound" "$trace"
}

# ------------------------------------------------------------------------------
# Build the categorized, actionability-ordered summary (R5) from the classified
# rows. Reads bucket<TAB>count<TAB>lastnr<TAB>path (+ a META fatal row).
# ------------------------------------------------------------------------------
_discovery_summary() {
  local failures="$1" probing="$2" fatal
  fatal=$(awk -F'\t' '$1=="META"&&$2=="fatal"{print $3}' "$failures"); fatal=${fatal:-0}

  echo "# Discovery summary for '$TARGET_BIN'  (errnos: ${DISCOVERY_ERRNOS// //})"
  echo "# count  path     — failures.log is the raw source of truth"

  # 1. Most likely culprits (just before exit): WRITE/APP/WALK near the crash.
  if [ "$fatal" -gt 0 ]; then
    local culprits
    culprits=$(awk -F'\t' -v F="$fatal" '($1=="WRITE"||$1=="APP"||$1=="WALK") && $3<=F {print $3"\t"$2"\t"$4}' "$failures" \
               | sort -rn -k1,1 | head -15 | awk -F'\t' '{printf "%6d  %s\n",$2,$3}')
    if [ -n "$culprits" ]; then echo; echo "== Most likely culprits (just before exit) =="; echo "$culprits"; fi
  fi

  _discovery_section "$failures" WRITE "App tried to CREATE/WRITE here (wants rw --bind)"
  _discovery_section "$failures" APP   "App data/config candidates"
  _discovery_section "$failures" WALK  "Ancestor-walk dotfile probes (usually noise)"
  _discovery_section "$failures" SYS   "System/libc config (usually optional)"
  _discovery_section "$failures" PATHL "PATH binary lookups (almost always harmless)"

  if [ -s "$probing" ]; then
    echo; echo "== Probing (failed then later succeeded — ignored) =="
    echo "  $(wc -l < "$probing") path(s); see probing.log"
  fi
  if [ ! -s "$failures" ] || ! grep -qv $'^META\t' "$failures"; then
    echo; echo "(no missing-permission candidates found)"
  fi
}

_discovery_section() {
  local failures="$1" tag="$2" title="$3" rows
  rows=$(awk -F'\t' -v T="$tag" '$1==T{printf "%6d  %s\n",$2,$4}' "$failures" | sort -k2)
  if [ -n "$rows" ]; then echo; echo "== $title =="; echo "$rows"; fi
}

# ------------------------------------------------------------------------------
# Print-only Level-1 suggestions for WRITE+APP buckets (Decisions #11, #12):
# bounded dir aggregation (one segment under a known root), $HOME-relativized,
# trailing-slash dirs; WRITE rows hint at rw --bind. Never edits the config.
# ------------------------------------------------------------------------------
_discovery_suggest() {
  local failures="$1" kind path rel
  echo "# Effective modules for $TARGET_BIN: $EFFECTIVE_DEPS"
  echo "# Paste into the most appropriate module in: $SANDBOX_CONFIG"
  echo "# WRITE entries usually need rw --bind; others default to --bind-try."
  awk -F'\t' -v home="$HOME" '
    $1!="WRITE" && $1!="APP" { next }
    function relroot(p,   R,i,base,rest,s){
      split(".config .local/share .local/state .cache", R, " ")
      for(i=1;i<=length(R);i++){ base=home"/"R[i]"/"; if(index(p,base)==1){ rest=substr(p,length(base)+1); split(rest,s,"/"); if(s[1]!="") return base s[1] } }
      return ""
    }
    { p=$4; agg=relroot(p); if(agg!="") DIR[agg]=1; else RAW[p]=1 }
    END { for(d in DIR) print "DIR\t"d; for(r in RAW) print "RAW\t"r }
  ' "$failures" | sort -u | while IFS="$(printf '\t')" read -r kind path; do
    rel="${path/#$HOME/\$HOME}"
    if [ "$kind" = "DIR" ]; then printf '  --bind-try "%s/" "%s/"\n' "$rel" "$rel"
    else printf '  --bind-try "%s" "%s"\n' "$rel" "$rel"; fi
  done
}
