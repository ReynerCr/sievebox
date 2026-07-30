# shellcheck shell=bash

# Complete a --flag=<value> argument with comma-separated multi-segment
# completion.  Called from _sievebox_complete below.
#   $1  flag prefix e.g. --relax=
#   $2  sievebox __complete subcommand e.g. "relax"
#   $3  the other flag prefix e.g. --modules= (prevents overlap)
_sievebox_complete_value() {
    local flag="$1" cmd="$2" other="$3"
    if [[ $prefix != *"$flag"* ]]; then
        return 1
    fi
    local suffix="${prefix##*"$flag"}"
    if [[ $suffix == *"$other"* ]]; then
        return 1
    fi
    local value="$suffix"
    local last="${value##*,}"
    local head=""
    if [[ $value == *,* ]]; then
        head="${value%,*},"
    fi
    local comps
    comps=$(sievebox __complete "$cmd" 2>/dev/null) || return 1
    COMPREPLY=($(compgen -W "$comps" -- "$last"))
    if [[ -n $head ]]; then
        COMPREPLY=("${COMPREPLY[@]/#/$head}")
    fi
    if [[ $cur == "$flag"* ]]; then
        COMPREPLY=("${COMPREPLY[@]/#/$flag}")
    fi
    compopt -o nospace 2>/dev/null
    return 0
}

_sievebox_complete() {
    local cur prefix

    # _init_completion re-parses COMP_LINE without = as a word break,
    # so --relax=filesystem stays as one word.  Available when
    # bash-completion is installed (interactive shells).
    # Falls back to raw COMP_WORDS + COMP_LINE parsing.
    if type _init_completion &>/dev/null; then
        _init_completion || return
    else
        cur="${COMP_WORDS[COMP_CWORD]}"
    fi

    prefix="${COMP_LINE:0:COMP_POINT}"

    # --relax=<value> and --modules=<value> (comma-separated)
    _sievebox_complete_value "--relax=" "relax" "--modules=" && return
    _sievebox_complete_value "--modules=" "modules" "--relax=" && return

    # Flag names
    if [[ $cur == -* ]]; then
        COMPREPLY=($(compgen -W "$(sievebox __complete flags 2>/dev/null)" -- "$cur"))
        if [[ ${#COMPREPLY[@]} -eq 1 && "${COMPREPLY[0]}" == *= ]]; then
            compopt -o nospace 2>/dev/null
        fi
        return 0
    fi

    # App names
    COMPREPLY=($(compgen -W "$(sievebox __complete apps 2>/dev/null)" -- "$cur"))
} &&
complete -F _sievebox_complete sievebox
