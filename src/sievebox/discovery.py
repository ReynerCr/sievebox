"""Permission-discovery engine: trace sandboxed app, classify missing paths."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING

from .bwrap import arity, category

if TYPE_CHECKING:
    from .config import Config

# --- Classification tables (env-overridable) ---------------------------------

ERRNOS = os.environ.get("DISCOVERY_ERRNOS", "ENOENT EACCES EROFS").split()

SYS_PATHS = os.environ.get(
    "DISCOVERY_SYS_PATHS",
    "/etc/localtime /etc/passwd /etc/group /etc/shadow /etc/nsswitch.conf "
    "/etc/host.conf /etc/hosts /etc/resolv.conf /etc/netsvc.conf "
    "/etc/ld.so.preload /etc/ld.so.cache /etc/machine-id /etc/os-release "
    "/etc/lsb-release /etc/timezone /etc/openssl /etc/ssl /etc/pki "
    "/etc/gtk-2.0 /etc/gtk-3.0 /etc/fonts",
).split()

CACHE_PATTERNS = os.environ.get(
    "DISCOVERY_CACHE_PATTERNS",
    "/.cache/ /var/cache/ /_cacache/ node-compile-cache /.cache-loader/",
).split()

DEPS_PATTERNS = os.environ.get("DISCOVERY_DEPS_PATTERNS", "/node_modules/").split()

# --- Project detection rules --------------------------------------------------

DETECT_RULES = [
    r.split("|") for r in os.environ.get(
        "SIEVEBOX_DETECT_RULES",
        "\n".join([
            "package.json|node|node",
            "Cargo.toml|rust|dev_base",
            "pyproject.toml|python|conda",
            "requirements.txt|python|conda",
            "environment.yml|python|conda",
            "go.mod|go|",
            "deno.json|deno|",
            "tauri.conf.json|tauri|",
        ]),
    ).strip().split("\n") if r.strip()
]

_AUTO_DETECT = os.environ.get("SIEVEBOX_AUTO_DETECT", "true") == "true"


# --- Bwrap arg vector parsing ------------------------------------------------

def iter_directives(bwrap_args: list[str]) -> list[tuple[str, list[str]]]:
    """Yield (flag, operands) tuples from a flat bwrap argument vector."""
    out: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(bwrap_args):
        flag = bwrap_args[i]
        n = arity(flag)
        out.append((flag, bwrap_args[i + 1 : i + n]))
        i += n
    return out


def extract_bound_paths(bwrap_args: list[str]) -> set[str]:
    """Dest paths populated from host: binds, symlinks, /dev, /proc.
    Excludes --tmpfs (root tmpfs is why unbound paths fail)."""
    out: set[str] = set()
    for flag, ops in iter_directives(bwrap_args):
        cat = category(flag)
        if cat in ("bind_rw", "bind_ro", "bind_dev", "bind_overlay"):
            if flag.endswith("-try"):
                if os.path.exists(ops[0]):
                    out.add(ops[1])
            else:
                out.add(ops[1])
        elif cat == "symlink":
            out.add(ops[1])
        elif cat == "virtual_fs":
            out.add(ops[0])
    out.add("/dev")
    out.add("/proc")
    return out


def extract_tmpfs_paths(bwrap_args: list[str]) -> set[str]:
    """Non-root --tmpfs destinations (e.g. /tmp, /run). Feeds EPHEM bucket."""
    out: set[str] = set()
    for flag, ops in iter_directives(bwrap_args):
        if category(flag) == "tmpfs":
            dst = ops[0]
            if dst and dst != "/":
                out.add(dst)
    return out


# --- Project detection --------------------------------------------------------

def project_hints(here: str, effective_deps: list[str]) -> str:
    """Advisory project-type detection. Returns multi-line string or empty."""
    if not _AUTO_DETECT:
        return ""
    types: list[str] = []
    gaps: list[str] = []
    eff_set = set(effective_deps)
    for rule in DETECT_RULES:
        marker, ptype = rule[0], rule[1]
        mod = rule[2] if len(rule) > 2 else ""
        if not os.path.exists(os.path.join(here, marker)):
            continue
        if ptype not in types:
            types.append(ptype)
        if mod and mod not in eff_set and mod not in gaps:
            gaps.append(mod)
    if not types:
        return ""
    lines = [f"[detect] Project in {here} looks like: {' '.join(types)}"]
    if gaps:
        lines.append(f"[detect] Active modules ({' '.join(effective_deps)}) may be MISSING: {' '.join(gaps)}")
        lines.append("[detect] If the app can't find those tools, watch the summary below")
        lines.append(f"[detect] for related paths (or add the module to the profile).")
    else:
        lines.append("[detect] All detected types are covered by active modules.")
    return "\n".join(lines)


# --- Strace trace classifier --------------------------------------------------

_PATH_RE = re.compile(r'"(/[^"]*)"')
_ERR_RE = re.compile(r"= -1 ([A-Z]+)")
_PID_RE = re.compile(r"^(\d+)\s+")
_UNFINISHED_RE = re.compile(r"<unfinished \.\.\.>$")
_RESUMED_RE = re.compile(r"<\.\.\. [a-z0-9_]+ resumed>")
_FATAL_RE = re.compile(r"\+\+\+ exited with [1-9]")
_SIG_RE = re.compile(r"--- SIG(SEGV|ABRT|BUS|KILL|FPE|ILL) ")

_WRITE_SYSCALLS = re.compile(
    r"(mkdir|mkdirat|creat|rmdir|unlink|unlinkat|rename|renameat|renameat2|"
    r"link|linkat|symlink|symlinkat|truncate|mknod|mknodat|chmod|fchmodat|"
    r"chown|lchown|fchownat)\("
)
_WRITE_OPEN_RE = re.compile(r"open(at)?\(")


def _get_path(line: str) -> str:
    m = _PATH_RE.search(line)
    return m.group(1) if m else ""


def _is_fail(line: str) -> bool:
    m = _ERR_RE.search(line)
    return m is not None and m.group(1) in ERRNOS


def _is_success(line: str) -> bool:
    if "= -1 " in line:
        return False
    if re.search(r"= \d", line):
        return True
    if re.search(r"= 0x", line):
        return True
    return False


def _is_write(line: str) -> bool:
    if _WRITE_SYSCALLS.search(line):
        return True
    if _WRITE_OPEN_RE.search(line) and re.search(r"O_CREAT|O_WRONLY|O_RDWR", line):
        return True
    return False


def _under(path: str, prefixes: set[str]) -> bool:
    if path in prefixes:
        return True
    parts = path.split("/")
    acc = ""
    for p in parts[1:]:
        acc = acc + "/" + p
        if acc in prefixes:
            return True
    return False


def _parent(path: str) -> str:
    k = path.rsplit("/", 1)[0]
    return k if k else "/"


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_ancestor(par: str, here: str) -> bool:
    return par == here or here.startswith(par + "/")


def _has_substring(path: str, patterns: list[str]) -> bool:
    return any(p in path for p in patterns)


def _parse_trace(trace_path: str) -> tuple[dict[str, int], dict[str, int], dict[str, int], set[str], int]:
    """Parse strace trace line by line.
    Returns (fail_count, last_seen, success, write_paths, fatal_line).
    """
    fail_count: dict[str, int] = {}
    last_seen: dict[str, int] = {}
    success: dict[str, int] = {}
    write_paths: set[str] = set()
    pending_path: dict[str, str] = {}
    pending_write: dict[str, bool] = {}
    fatal = 0
    tnr = 0

    with open(trace_path, errors="replace") as fh:
        for raw in fh:
            tnr += 1
            line = raw.rstrip("\n")
            m = _PID_RE.match(line)
            pid = m.group(1) if m else "-"
            if m:
                line = line[m.end():]

            if fatal == 0 and (_FATAL_RE.search(line) or _SIG_RE.search(line)):
                fatal = tnr

            if _UNFINISHED_RE.search(line):
                p = _get_path(line)
                if p:
                    pending_path[pid] = p
                    pending_write[pid] = _is_write(line)
                continue
            if _RESUMED_RE.search(line):
                p = pending_path.get(pid, "")
                w = pending_write.get(pid, False)
                pending_path.pop(pid, None)
                pending_write.pop(pid, None)
            else:
                p = _get_path(line)
                w = _is_write(line)

            if not p or not p.startswith("/"):
                continue
            p = re.sub(r"/+", "/", p)

            if _is_fail(line):
                fail_count[p] = fail_count.get(p, 0) + 1
                last_seen[p] = tnr
                if w:
                    write_paths.add(p)
            elif _is_success(line):
                success[p] = success.get(p, 0) + 1

    return fail_count, last_seen, success, write_paths, fatal


def _classify_paths(fail_count: dict[str, int], last_seen: dict[str, int],
                    success: dict[str, int], write_paths: set[str],
                    fatal: int, bound: set[str], tmpfs: set[str],
                    here: str, path_env: str) -> tuple[list[dict], list[dict]]:
    """Classify each failed path into buckets. Returns (failures, probing)."""
    path_set: set[str] = set()
    for d in path_env.split(":"):
        d = re.sub(r"/+", "/", d)
        if len(d) > 1:
            d = d.rstrip("/")
        if d:
            path_set.add(d)

    # Pass 1: gather candidates + prelim bucket
    bnc: dict[str, int] = {}
    bnc_seen: set[str] = set()
    candidates: dict[str, str] = {}
    probing: list[dict] = []

    for p in fail_count:
        if success.get(p, 0) > 0 and p not in write_paths:
            probing.append({"path": p, "fails": fail_count[p], "successes": success[p]})
            continue
        if _under(p, bound):
            continue
        if _under(p, tmpfs):
            candidates[p] = "EPHEM"
        elif p in write_paths:
            candidates[p] = "WRITE"
        elif _parent(p) in path_set:
            candidates[p] = "PATHL"
        elif _has_substring(p, DEPS_PATTERNS):
            candidates[p] = "DEPS"
        elif _has_substring(p, CACHE_PATTERNS):
            candidates[p] = "CACHE"
        elif _under(p, set(SYS_PATHS)):
            candidates[p] = "SYS"
        else:
            candidates[p] = "?"
            par = _parent(p)
            if _is_ancestor(par, here):
                key = _basename(p)
                combo = key + "\0" + par
                if combo not in bnc_seen:
                    bnc_seen.add(combo)
                    bnc[key] = bnc.get(key, 0) + 1

    # Pass 2: finalize WALK vs APP
    failures: list[dict] = []
    for p in candidates:
        b = candidates[p]
        if b == "?":
            par = _parent(p)
            b = "WALK" if (bnc.get(_basename(p), 0) >= 2 and _is_ancestor(par, here)) else "APP"
        failures.append({
            "bucket": b,
            "count": fail_count[p],
            "last": last_seen[p],
            "path": p,
        })

    failures.sort(key=lambda r: r["path"])
    failures.append({"bucket": "META", "count": "fatal", "last": fatal, "path": "-"})
    return failures, probing


def classify(trace_path: str, bound: set[str], tmpfs: set[str],
             here: str, path_env: str) -> tuple[list[dict], list[dict]]:
    """Parse strace trace, classify failures into buckets.
    Returns (failures, probing) lists of dicts."""
    fail_count, last_seen, success, write_paths, fatal = _parse_trace(trace_path)
    return _classify_paths(fail_count, last_seen, success, write_paths,
                           fatal, bound, tmpfs, here, path_env)


# --- Mark exists --------------------------------------------------------------

def mark_exists(failures: list[dict]) -> None:
    """Annotate each row with 'E' (exists on host) or 'M' (missing). In-place."""
    for row in failures:
        if row["bucket"] == "META":
            row["exists"] = ""
        else:
            row["exists"] = "E" if os.path.exists(row["path"]) else "M"


# --- Summary builder ----------------------------------------------------------

def build_summary(failures: list[dict], probing: list[dict],
                  detect_text: str, target_bin: str) -> str:
    """Build the categorized, actionability-ordered summary."""
    fatal = 0
    for r in failures:
        if r["bucket"] == "META" and r["count"] == "fatal":
            fatal = r["last"]
            break

    lines = [
        f"# Discovery summary for '{target_bin}'  (errnos: {'/'.join(ERRNOS)})",
        "# [exists]=on host, a real bind candidate;  [missing]=app probe, not on disk",
        "# count  tag  path    -> failures.log is the raw source of truth",
    ]

    if detect_text.strip():
        lines.append("")
        lines.append("== Project detection ==")
        lines.append(detect_text.rstrip())

    # Most likely culprits
    if fatal > 0:
        culprits = [
            r for r in failures
            if r["bucket"] in ("WRITE", "APP", "WALK") and r["last"] <= fatal
        ]
        culprits.sort(key=lambda r: r["last"], reverse=True)
        culprits = culprits[:15]
        if culprits:
            lines.append("")
            lines.append("== Most likely culprits (just before exit) ==")
            for r in culprits:
                lab = "[exists]" if r["exists"] == "E" else "[missing]"
                lines.append(f"{r['count']:6d}  {lab:<9s} {r['path']}")

    # Actionable buckets
    _section(lines, failures, "WRITE", "App tried to CREATE/WRITE here (wants rw --bind)", 50)
    _section(lines, failures, "APP", "App data/config candidates", 50)
    _section(lines, failures, "WALK", "Ancestor-walk dotfile probes (usually noise)", 20)
    # Well-understood buckets
    _section(lines, failures, "DEPS", "node_modules lookups (missing package? not installed)", 15)
    _section(lines, failures, "CACHE", "Regenerable cache dirs (safe to ignore)", 12)
    _section(lines, failures, "EPHEM", "Ephemeral sandbox tmpfs (regenerated; do NOT bind)", 12)
    _section(lines, failures, "SYS", "System/libc config (usually optional)", 15)
    _section(lines, failures, "PATHL", "PATH binary lookups (almost always harmless)", 12)

    if probing:
        lines.append("")
        lines.append("== Probing (failed then later succeeded -> ignored) ==")
        lines.append(f"  {len(probing)} path(s); see probing.log")

    real_failures = [r for r in failures if r["bucket"] != "META"]
    if not real_failures:
        lines.append("")
        lines.append("(no missing-permission candidates found)")

    return "\n".join(lines)


def _section(lines: list[str], failures: list[dict],
             tag: str, title: str, cap: int) -> None:
    rows = [r for r in failures if r["bucket"] == tag]
    total = len(rows)
    if total == 0:
        return
    rows.sort(key=lambda r: (0 if r["exists"] == "E" else 1, r["path"]))
    lines.append("")
    lines.append(f"== {title}  ({total}) ==")
    for r in rows[:cap]:
        lab = "[exists]" if r["exists"] == "E" else "[missing]"
        lines.append(f"{r['count']:6d}  {lab:<9s} {r['path']}")
    if total > cap:
        lines.append(f"  ... (+{total - cap} more; see failures.log)")


# --- Orchestration ------------------------------------------------------------

def run_discovery(cfg: Config, target: str, bwrap_argv: list[str],
                  bwrap_flat: list[str], pass_fds: tuple[int, ...],
                  here: str, home: str, state_dir: str,
                  effective_deps: list[str]) -> int:
    """Run strace+bwrap via fd-based argv, classify from flat args, summarize. Returns exit code."""
    if not which("strace"):
        print("Error: --discover requires 'strace' (not found on PATH).", file=sys.stderr)
        return 1

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(state_dir) / "discovery" / f"{target}-{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(Path(state_dir), 0o700)
    except OSError:
        pass

    trace = run_dir / "trace.raw"
    failures_path = run_dir / "failures.log"
    probing_path = run_dir / "probing.log"
    summary_path = run_dir / "summary.txt"
    bound_path = run_dir / "bound_paths.txt"
    tmpfs_path = run_dir / "tmpfs_paths.txt"
    detect_path = run_dir / "detect.txt"

    bound = extract_bound_paths(bwrap_flat)
    tmpfs = extract_tmpfs_paths(bwrap_flat)
    bound_path.write_text("\n".join(sorted(bound)) + "\n" if bound else "")
    tmpfs_path.write_text("\n".join(sorted(tmpfs)) + "\n" if tmpfs else "")

    print()
    detect_text = project_hints(here, effective_deps)
    detect_path.write_text(detect_text + "\n" if detect_text else "")
    if detect_text:
        print(detect_text)
    print(f"[discovery] Tracing '{target}'. Use it normally, then exit to analyze.")
    print(f"[discovery] Artifacts: {run_dir}")
    print()

    rc = _run_strace(trace, bwrap_argv, pass_fds)

    failures, probing = classify(str(trace), bound, tmpfs, here, os.environ.get("PATH", ""))
    mark_exists(failures)

    _write_failures(failures, failures_path)
    _write_probing(probing, probing_path)

    summary = build_summary(failures, probing, detect_text, target)
    summary_path.write_text(summary + "\n")

    print()
    if rc != 0:
        print(f"[discovery] '{target}' exited with code {rc}.")
    else:
        print(f"[discovery] '{target}' exited cleanly.")
    print("=" * 68)
    print(summary)
    print("=" * 68)
    print(f"[discovery] Full artifacts kept in: {run_dir}")

    return rc


def _run_strace(trace_path: Path, bwrap_argv: list[str],
                pass_fds: tuple[int, ...]) -> int:
    try:
        subprocess.run(
            ["strace", "-f", "-e", "trace=%file", "-o", str(trace_path),
             "bwrap", *bwrap_argv],
            pass_fds=pass_fds,
        )
    except subprocess.CalledProcessError as e:
        return e.returncode
    return 0


def _write_failures(failures: list[dict], path: Path) -> None:
    lines = []
    for r in failures:
        if r["bucket"] == "META":
            lines.append(f"META\t{r['count']}\t{r['last']}\t{r['path']}")
        else:
            lines.append(f"{r['bucket']}\t{r['count']}\t{r['last']}\t{r['path']}\t{r['exists']}")
    path.write_text("\n".join(lines) + "\n" if lines else "")


def _write_probing(probing: list[dict], path: Path) -> None:
    lines = [f"{r['path']}\t{r['fails']}\t{r['successes']}" for r in probing]
    path.write_text("\n".join(lines) + "\n" if lines else "")
