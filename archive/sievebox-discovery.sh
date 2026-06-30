# shellcheck shell=bash
# ==============================================================================
# sievebox-discovery.sh -> optional permission-discovery engine for sievebox
# ==============================================================================
# Sourced by `sievebox` ONLY in --discover mode. Runs the EXACT same sandbox
# under host-side strace (following forks), then classifies the paths the app
# could not access into actionable CATEGORIES (see PLAN.md "Phase 6").
#
# Engine guarantees these are defined when sourced: extract_bound_paths(),
# extract_tmpfs_paths(), TARGET_BIN, EFFECTIVE_DEPS, SIEVEBOX_CONFIG,
# SIEVEBOX_STATE_DIR, HERE, HOME, PATH.
# ==============================================================================

# Errno failures treated as candidates. EROFS is included so write attempts that
# fail because the root is a tmpfs (the usual crash cause) are captured.
DISCOVERY_ERRNOS="${DISCOVERY_ERRNOS:-ENOENT EACCES EROFS}"

# --- Rule-based classification table -------------------------------------
# Declarative path->category rules. SYS uses PREFIX matching (entry == path or a
# parent of it); CACHE/DEPS use SUBSTRING matching. All are env-overridable so
# users can extend the taxonomy without touching the awk classifier.
#   SYS    known system/libc config        -> "usually optional"
#   CACHE  regenerable cache dirs          -> "ignore, regenerated"
#   DEPS   node_modules (sans .bin)        -> "missing package? not installed"
# tmpfs destinations are discovered dynamically and form the EPHEM bucket.
DISCOVERY_SYS_PATHS="${DISCOVERY_SYS_PATHS:-/etc/localtime /etc/passwd /etc/group /etc/shadow /etc/nsswitch.conf /etc/host.conf /etc/hosts /etc/resolv.conf /etc/netsvc.conf /etc/ld.so.preload /etc/ld.so.cache /etc/machine-id /etc/os-release /etc/lsb-release /etc/timezone /etc/openssl /etc/ssl /etc/pki /etc/gtk-2.0 /etc/gtk-3.0 /etc/fonts}"
DISCOVERY_CACHE_PATTERNS="${DISCOVERY_CACHE_PATTERNS:-/.cache/ /var/cache/ /_cacache/ node-compile-cache /.cache-loader/}"
DISCOVERY_DEPS_PATTERNS="${DISCOVERY_DEPS_PATTERNS:-/node_modules/}"

# --- Project-type detection (Phase 4, advisory) -------------------------------
# Advisory only (never touches the sandbox); opt out with SIEVEBOX_AUTO_DETECT=false.
# Rules are "marker|type|module" (module optional): a marker file in $HERE flags
# <type>; if <module> is set and missing from EFFECTIVE_DEPS, it's reported as a gap.
SIEVEBOX_AUTO_DETECT="${SIEVEBOX_AUTO_DETECT:-true}"
SIEVEBOX_DETECT_RULES="${SIEVEBOX_DETECT_RULES:-
package.json|node|node
Cargo.toml|rust|dev_base
pyproject.toml|python|conda
requirements.txt|python|conda
environment.yml|python|conda
go.mod|go|
deno.json|deno|
tauri.conf.json|tauri|
}"

# Print an advisory "expected vs covered" heads-up before tracing (Phase 4).
_discovery_project_hints() {
  [ "$SIEVEBOX_AUTO_DETECT" = "true" ] || return 0
  local marker type mod types="" gaps=""
  while IFS='|' read -r marker type mod; do
    [ -n "$marker" ] || continue
    [ -e "$HERE/$marker" ] || continue
    case " $types " in *" $type "*) ;; *) types+="$type " ;; esac
    if [ -n "$mod" ] && ! grep -qw -- "$mod" <<<"$EFFECTIVE_DEPS"; then
      case " $gaps " in *" $mod "*) ;; *) gaps+="$mod " ;; esac
    fi
  done <<EOF
$SIEVEBOX_DETECT_RULES
EOF
  [ -n "$types" ] || return 0
  echo "[detect] Project in $HERE looks like: ${types% }"
  if [ -n "$gaps" ]; then
    echo "[detect] Active modules ($EFFECTIVE_DEPS) may be MISSING: ${gaps% }"
    echo "[detect] If the app can't find those tools, watch the summary below"
    echo "[detect] for related paths (or add the module to $TARGET_BIN's profile)."
  else
    echo "[detect] All detected types are covered by active modules."
  fi
}

run_discovery() {
  if ! command -v strace >/dev/null 2>&1; then
    echo "Error: --discover requires 'strace' (not found on PATH)." >&2
    return 1
  fi

  local ts run_dir trace failures probing summary bound tmpfs detect rc=0
  ts="$(date +%Y%m%d-%H%M%S)"
  run_dir="$SIEVEBOX_STATE_DIR/discovery/${TARGET_BIN}-${ts}"
  mkdir -p "$run_dir"
  chmod 700 "$SIEVEBOX_STATE_DIR" 2>/dev/null || true
  trace="$run_dir/trace.raw"
  failures="$run_dir/failures.log"   # rows: bucket<TAB>count<TAB>lastnr<TAB>path<TAB>E|M
  probing="$run_dir/probing.log"
  summary="$run_dir/summary.txt"
  bound="$run_dir/bound_paths.txt"
  tmpfs="$run_dir/tmpfs_paths.txt"
  detect="$run_dir/detect.txt"        # Phase 4 project-detection result (persisted)

  extract_bound_paths "$@" | sort -u > "$bound"
  extract_tmpfs_paths "$@" | sort -u > "$tmpfs"

  echo
  # Advisory heads-up; tee so it's shown now AND persisted/folded into the summary.
  _discovery_project_hints | tee "$detect"
  echo "[discovery] Tracing '$TARGET_BIN'. Use it normally, then exit to analyze."
  echo "[discovery] Artifacts: $run_dir"
  echo

  strace -f -e trace=%file -o "$trace" bwrap "$@" || rc=$?

  : > "$failures"
  : > "$probing"
  _discovery_classify "$trace" "$bound" "$tmpfs" "$failures" "$probing"
  _discovery_mark_exists "$failures"   # annotate each row with host existence

  _discovery_summary "$failures" "$probing" "$detect" > "$summary"

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
#   EPHEM  under a writable sandbox tmpfs -> regenerated scratch
#   WRITE  app tried to create/write (EROFS/EACCES on a write syscall)
#   PATHL  parent dir is a $PATH entry -> binary lookup
#   DEPS   under node_modules (missing package?)
#   CACHE  regenerable cache dir
#   SYS    known system/libc config (/etc/...)
#   WALK   same basename failing across >=2 ancestor dirs of $HERE
#   APP    everything else (the real signal)
# Also records last-seen order per path and the first fatal-exit position for
# the "most likely culprits" view. Probing (failed-then-succeeded) -> PROB.
# ------------------------------------------------------------------------------
_discovery_classify() {
  local trace="$1" bound="$2" tmpfs="$3" failures="$4" probing="$5"
  awk -v errnos="$DISCOVERY_ERRNOS" -v FAIL="$failures" -v PROB="$probing" \
      -v here="$HERE" -v pathenv="$PATH" -v tmpfsfile="$tmpfs" \
      -v syspaths="$DISCOVERY_SYS_PATHS" -v cachepats="$DISCOVERY_CACHE_PATTERNS" \
      -v depspats="$DISCOVERY_DEPS_PATTERNS" '
    BEGIN {
      ne = split(errnos, ea, " "); for (i=1;i<=ne;i++) ERR[ea[i]]=1
      # PATH entries -> set (normalized: collapse //, strip trailing /)
      np = split(pathenv, pa, ":")
      for (i=1;i<=np;i++){ d=pa[i]; gsub(/\/+/,"/",d); if(length(d)>1) sub(/\/$/,"",d); if(d!="") PSET[d]=1 }
      # rule tables: SYS prefixes, CACHE/DEPS substrings
      ns = split(syspaths, sa, " "); for (i=1;i<=ns;i++) SYSP[sa[i]]=1
      nc = split(cachepats, ca, " "); for (i=1;i<=nc;i++) CACHEP[i]=ca[i]; ncp=nc
      nd = split(depspats, da, " "); for (i=1;i<=nd;i++) DEPSP[i]=da[i]; ndp=nd
      # tmpfs destinations -> EPHEM prefixes (root "/" already excluded)
      while ((getline line < tmpfsfile) > 0) { if (line!="") TMPFS[line]=1 }
      close(tmpfsfile)
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
    function under(p,SET,   n,parts,i,acc){ if(p in SET) return 1; n=split(p,parts,"/"); acc=""; for(i=2;i<=n;i++){acc=acc"/"parts[i]; if(acc in SET) return 1} return 0 }
    function is_bound(p){ return under(p,BOUND) }
    function is_ephem(p){ return under(p,TMPFS) }
    function is_sys(p){ return under(p,SYSP) }
    function has_sub(p,ARR,n,   i){ for(i=1;i<=n;i++) if(index(p,ARR[i])>0) return 1; return 0 }
    function is_cache(p){ return has_sub(p,CACHEP,ncp) }
    function is_deps(p){ return has_sub(p,DEPSP,ndp) }
    function parent(p,   k){ k=p; sub(/\/[^/]*$/,"",k); if(k=="")k="/"; return k }
    function base(p,   k){ k=p; sub(/.*\//,"",k); return k }
    function is_ancestor(par){ return (par==here || index(here"/", par"/")==1) }

    END {
      # pass 1: gather candidates + prelim bucket; collect ancestor-walk stats
      for (p in FAILC) {
        if (SUCC[p]>0 && !(p in WR)) { printf "%s\t%d\t%d\n", p, FAILC[p], SUCC[p] > PROB; continue }
        if (is_bound(p)) continue
        CAND[p]=1
        if (is_ephem(p)) B[p]="EPHEM"
        else if (p in WR) B[p]="WRITE"
        else if (parent(p) in PSET) B[p]="PATHL"
        else if (is_deps(p)) B[p]="DEPS"
        else if (is_cache(p)) B[p]="CACHE"
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
  ' "$bound" "$trace"
}

# ------------------------------------------------------------------------------
# tag each row with host existence (5th field E|M). [exists] = real fix
# candidate (host has it, sandbox lacked a bind); [missing] = app probe for a
# nonexistent path. One stat per row, host-side. META row passes through.
# ------------------------------------------------------------------------------
_discovery_mark_exists() {
  local f="$1" tmp="$1.tmp" bucket count last path
  : > "$tmp"
  while IFS=$'\t' read -r bucket count last path; do
    if [ "$bucket" = "META" ]; then
      printf '%s\t%s\t%s\t%s\n' "$bucket" "$count" "$last" "$path" >> "$tmp"
      continue
    fi
    if [ -e "$path" ]; then
      printf '%s\t%s\t%s\t%s\tE\n' "$bucket" "$count" "$last" "$path" >> "$tmp"
    else
      printf '%s\t%s\t%s\t%s\tM\n' "$bucket" "$count" "$last" "$path" >> "$tmp"
    fi
  done < "$f"
  mv "$tmp" "$f"
}

# ------------------------------------------------------------------------------
# Build the categorized, actionability-ordered summary from the
# classified rows. Reads bucket<TAB>count<TAB>lastnr<TAB>path<TAB>E|M (+ META).
# ------------------------------------------------------------------------------
_discovery_summary() {
  local failures="$1" probing="$2" detect="${3:-}" fatal
  fatal=$(awk -F'\t' '$1=="META"&&$2=="fatal"{print $3}' "$failures"); fatal=${fatal:-0}

  echo "# Discovery summary for '$TARGET_BIN'  (errnos: ${DISCOVERY_ERRNOS// //})"
  echo "# [exists]=on host, a real bind candidate;  [missing]=app probe, not on disk"
  echo "# count  tag  path    -> failures.log is the raw source of truth"

  # Phase 4: include the project-detection heads-up so the summary is self-contained.
  if [ -n "$detect" ] && [ -s "$detect" ]; then
    echo; echo "== Project detection =="; cat "$detect"
  fi

  # 1. Most likely culprits (just before exit): WRITE/APP/WALK near the crash.
  if [ "$fatal" -gt 0 ]; then
    local culprits
    culprits=$(awk -F'\t' -v F="$fatal" '($1=="WRITE"||$1=="APP"||$1=="WALK") && $3<=F {lab=($5=="E"?"[exists] ":"[missing]"); print $3"\t"$2"\t"lab"\t"$4}' "$failures" \
               | sort -rn -k1,1 | head -15 | awk -F'\t' '{printf "%6d  %-9s %s\n",$2,$3,$4}')
    if [ -n "$culprits" ]; then echo; echo "== Most likely culprits (just before exit) =="; echo "$culprits"; fi
  fi

  # Actionable buckets (full, [exists]-first, generous cap).
  _discovery_section "$failures" WRITE "App tried to CREATE/WRITE here (wants rw --bind)" 50
  _discovery_section "$failures" APP   "App data/config candidates" 50
  _discovery_section "$failures" WALK  "Ancestor-walk dotfile probes (usually noise)" 20
  # Well-understood / high-volume buckets (tight cap; mostly informational).
  _discovery_section "$failures" DEPS  "node_modules lookups (missing package? not installed)" 15
  _discovery_section "$failures" CACHE "Regenerable cache dirs (safe to ignore)" 12
  _discovery_section "$failures" EPHEM "Ephemeral sandbox tmpfs (regenerated; do NOT bind)" 12
  _discovery_section "$failures" SYS   "System/libc config (usually optional)" 15
  _discovery_section "$failures" PATHL "PATH binary lookups (almost always harmless)" 12

  if [ -s "$probing" ]; then
    echo; echo "== Probing (failed then later succeeded -> ignored) =="
    echo "  $(wc -l < "$probing") path(s); see probing.log"
  fi
  if [ ! -s "$failures" ] || ! grep -qv $'^META\t' "$failures"; then
    echo; echo "(no missing-permission candidates found)"
  fi
}

# Print one bucket: [exists] rows first, then [missing], each path-sorted, with
# an existence tag. Capped at $4 rows; the remainder is noted (see
# failures.log for the full set).
_discovery_section() {
  local failures="$1" tag="$2" title="$3" cap="${4:-40}" total rows
  total=$(awk -F'\t' -v T="$tag" '$1==T{n++} END{print n+0}' "$failures")
  [ "$total" -eq 0 ] && return 0
  # sort key: existence (E=0 first, M=1) then path; then drop the key columns.
  rows=$(awk -F'\t' -v T="$tag" '$1==T{
           e=($5=="E"?0:1); lab=($5=="E"?"[exists]":"[missing]")
           printf "%d\t%s\t%6d  %-9s %s\n", e, $4, $2, lab, $4 }' "$failures" \
         | sort -t$'\t' -k1,1n -k2,2 | cut -f3- | head -n "$cap")
  echo; echo "== $title  (${total}) =="; echo "$rows"
  if [ "$total" -gt "$cap" ]; then echo "  ... (+$((total - cap)) more; see failures.log)"; fi
}

# ------------------------------------------------------------------------------
# Print-only Level-1 suggestions for WRITE+APP buckets:
# bounded dir aggregation (one segment under a known root), $HOME-relativized,
# trailing-slash dirs; WRITE rows hint at rw --bind. Never edits the config.
# ------------------------------------------------------------------------------
_discovery_suggest() {
  local failures="$1" kind path rel
  echo "# Effective modules for $TARGET_BIN: $EFFECTIVE_DEPS"
  echo "# Paste into the most appropriate module in: $SIEVEBOX_CONFIG"
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
