# shellcheck shell=bash
# ==============================================================================
# sandbox-profiles.sh — sandbox-run profile registry (DATA / configuration)
# ==============================================================================
# This file is *sourced* by the `sandbox-run` engine; it is not executed on its
# own. The engine guarantees the following are already defined when this file
# is sourced:
#   - register_module()                 (function)
#   - add_setenv()                      (function)
#   - MODULE_COLOR / MODULE_ENV_SCRIPT  (associative arrays)
#   - MODULE_SETENV                     (associative array)
#   - PROFILE_DEPS / PROFILE_ROOT_MOD   (associative arrays)
#   - OUTPUT_COLOR / RESET_COLOR        (tput color command prefixes)
#   - TARGET_BIN / HERE / HOME          (current invocation context)
#
# Discovery order used by the engine (first existing file wins):
#   1. $SANDBOX_RUN_CONFIG
#   2. sandbox-profiles.sh next to the sandbox-run script
#   3. ${XDG_CONFIG_HOME:-~/.config}/sandbox-run/profiles.sh
# ==============================================================================

# ==============================================================================
# HOST POLICY KNOBS
# ==============================================================================

# ** Apps allowed to start in $HOME. Evaluated as a REGEX alternation, so be
# ** careful. NOTE: these apps also do NOT get $HERE bound when $HERE is $HOME.
RUN_HOME_WHITELIST=(llama)

# ** Apps that are NOT allowed network access. Evaluated as a REGEX alternation.
NET_BLACKLIST=()

# ==============================================================================
# DECLARATIVE MODULE INVENTORY
# ==============================================================================
# Some cool colors that would work for new tools: 1: red
# You can view some more with following script:
# for c in {0..255}; do tput setaf $c; tput setaf $c | cat -v; echo " = Color $c"; done | column

# 1. Python / Conda Ecosystem Module
# Prompt Color: Bright orange (184)
CONDA_COLOR="184"
# CONDA_ENV drives the auto-activation logic in EXEC_CMD; forward it past --clearenv.
MODULE_SETENV["conda"]="CONDA_ENV"
register_module "conda" "$CONDA_COLOR" "
  export CONDA_EXE=\"/usr/bin/conda\"

  # Check if the execution context is strictly for a Python/Conda interactive shell session
  if [ \"\$0\" = \"conda\" ]; then
    # Setup official Conda functions for the login shell
    [ -f /etc/profile.d/conda.sh ] && source /etc/profile.d/conda.sh

    # If arguments were passed (e.g., 'activate pdf'), we drop the first arg ('conda')
    # and evaluate the rest ('activate pdf') directly in the context of this shell.
    if [ \"\$#\" -gt 1 ]; then
        shift
        conda \"\$@\"
    else
        echo -n \"Use '$($OUTPUT_COLOR $CONDA_COLOR)conda activate <env>$($RESET_COLOR) to activate an environment.'\"
    fi

    echo \"\"

    # Lock the user inside the persistent interactive container shell
    exec bash --login -i
  fi
" \
  --ro-bind-try "/etc/conda" "/etc/conda" \
  --bind-try "$HOME/.conda" "$HOME/.conda" \
  --bind-try "$HOME/.cache/conda" "$HOME/.cache/conda" \
  --bind-try "$HOME/.cache/pip" "$HOME/.cache/pip" \
  --bind-try "$HOME/.condarc" "$HOME/.condarc" \
  --bind-try "$HOME/.ipython" "$HOME/.ipython"

# 2. Node.js Ecosystem Module (npm, pnpm, yarn, node, bun)
# Prompt Color: Bright yellow (226)
# pnpm reads PNPM_HOME to locate its global store/bin; forward it past --clearenv.
MODULE_SETENV["node"]="PNPM_HOME"
# PNPM_HOME is optional; only bind it when the host actually defines it.
NODE_PNPM_ARGS=()
[ -n "${PNPM_HOME:-}" ] && NODE_PNPM_ARGS=(--bind-try "$PNPM_HOME" "$PNPM_HOME")

register_module "node" "226" "" \
  --ro-bind-try "$HOME/.npmrc" "$HOME/.npmrc" \
  "${NODE_PNPM_ARGS[@]}" \
  --bind-try "$HOME/.npm" "$HOME/.npm" \
  --bind-try "$HOME/.cache/pnpm" "$HOME/.cache/pnpm" \
  --bind-try "$HOME/.cache/node" "$HOME/.cache/node" \
  --bind-try "$HOME/.config/pnpm" "$HOME/.config/pnpm" \
  --bind-try "$HOME/.bun" "$HOME/.bun" \
  --bind-try "$HOME/.node_repl_history" "$HOME/.node_repl_history" \

PI_TARGET_DIR="$HOME/AppInstalls/custom-scripts/dev/pi-agent"

# 3a. Base development toolchain (shared by dev profiles via inheritance).
# Prompt Color: 2. Modules inherit this with MODULE_EXTENDS["x"]="dev_base".
register_module "dev_base" "2" "" \
  --bind-try "$HOME/.cargo" "$HOME/.cargo"

# 3b. DEVELOPMENT Isolation Module — extends dev_base (inherits its binds).
# Prompt Color: 2
MODULE_EXTENDS["webdev"]="dev_base"
register_module "webdev" "2" "" \
  --bind-try "$HOME/.cache/ms-playwright" "$HOME/.cache/ms-playwright" \
  --bind-try "$HOME/.cache/Cypress" "$HOME/.cache/Cypress" \
  --bind-try "$HOME/.config/nextjs-nodejs" "$HOME/.config/nextjs-nodejs" \
  --bind-try "$HOME/.config/create-next-app-nodejs" "$HOME/.config/create-next-app-nodejs"

# 4. Pi Agent Isolation Module
# Prompt Color: Bright light blue (81)
register_module "pi_agent" "81" "" \
  --bind-try "$HOME/.pi" "$HOME/.pi" \
  --bind-try "$PI_TARGET_DIR" "$PI_TARGET_DIR"

# 5. OpenCode Agent Isolation Module
# Prompt Color: Bright cyan (86)
register_module "opencode_agent" "86" "" \
  --bind-try "$HOME/.opencode" "$HOME/.opencode" \
  --bind-try "$HOME/.config/opencode" "$HOME/.config/opencode" \
  --bind-try "$HOME/.local/share/opencode" "$HOME/.local/share/opencode" \
  --bind-try "$HOME/.local/state/opencode" "$HOME/.local/state/opencode" \
  --bind-try "$HOME/.local/share/opentui" "$HOME/.local/share/opentui" \
  --bind-try "$HOME/.cache/opencode" "$HOME/.cache/opencode"

# 6. Llama.cpp (and related) Isolation Module
# Prompt Color: Purple (99)
# --- Requires GPU & Vulkan Acceleration Passthrough ---
register_module "llama_cpp" "99" "" \
  --ro-bind-try "$HOME/llamacpp/llama-cpp" "$HOME/llamacpp/llama-cpp" \
  --ro-bind-try "$HOME/llama.ini" "$HOME/llama.ini" \
  --bind-try "$HOME/.cache/llama.cpp/" "$HOME/.cache/llama.cpp/" \
  --bind-try "$HOME/.cache/huggingface" "$HOME/.cache/huggingface"

register_module "gpu" "1" "" \
  --ro-bind-try "$HOME/.cache/mesa_shader_cache" "$HOME/.cache/mesa_shader_cache" \
  --ro-bind-try "$HOME/.cache/radv_builtin_shaders" "$HOME/.cache/radv_builtin_shaders" \
  --dev-bind-try "/dev/dri" "/dev/dri" \
  --ro-bind-try "/sys/dev" "/sys/dev" \
  --ro-bind-try "/sys/devices" "/sys/devices" \
  --ro-bind-try "/etc/vulkan" "/etc/vulkan"

# 7. Simple Isolation Module
# Mainly used for testing with bash but can work for whatever else.
# Prompt Color: Soft purple (183)
register_module "simple_module" "183" ""

# 8. RTK
# Prompt Color: Opaque Blue (105)
register_module "rtk_ai" "105" "" \
  --bind-try "$HOME/.local/bin/rtk" "$HOME/.local/bin/rtk"

# 9. Devin CLI
# Prompt Color: Opaque Blue (105)
register_module "devin_cli" "105" "" \
  --bind-try "$HOME/.local/share/devin" "$HOME/.local/share/devin" \
  --bind-try "$HOME/.cache/devin" "$HOME/.cache/devin" \
  --bind-try "$HOME/.config/devin" "$HOME/.config/devin" \
  --bind-try "$HOME/.local/bin/devin" "$HOME/.local/bin/devin" \
  --bind-try "$HOME/.local/share/chisel" "$HOME/.local/share/chisel" \
  --bind-try "$HOME/.local/share/cognition" "$HOME/.local/share/cognition"

# 3. DEVELOPMENT Isolation Module
# Prompt Color: 2
register_module "specific_projects" "3" "" \
  --bind-try "$HOME/.local/share/com.zenoread.app" "$HOME/.local/share/com.zenoread.app"

  # THIS ONE ABOVE WAS NOT INTENDED FOR GENERAL PURPOSES BUT NEEDED FOR TESTING A LOCAL APP

# ** Add more modules here

# SOME folder that could be added later on are:
# JAVA FOLDERS THAT COULD BE GOOD FOR A NEW PROFILE IN THE FUTURE
#--ro-bind-try "/etc/java" "/etc/java"
#--ro-bind-try "/etc/jvm" "/etc/jvm"

# maybe, MAYBE, the git folders (right not it doesn't trust sandboxed apps)
#  --bind-try "$HOME/.config/git" "$HOME/.config/git" \
#  --bind-try "$HOME/.gitconfig" "$HOME/.gitconfig" \

# ==============================================================================
# BINARY TO PROFILE ROUTING MAP
# ==============================================================================
# ** Map each BINARY to the space-separated list of modules it should load.
PROFILE_DEPS["conda"]="conda webdev specific_projects"
PROFILE_DEPS["npm"]="node webdev gpu specific_projects"
PROFILE_DEPS["pnpm"]="node webdev specific_projects"
PROFILE_DEPS["yarn"]="node webdev specific_projects"
PROFILE_DEPS["npx"]="node webdev specific_projects"
PROFILE_DEPS["node"]="node webdev specific_projects"
PROFILE_DEPS["bun"]="node webdev specific_projects"
PROFILE_DEPS["pi"]="conda node webdev pi_agent rtk_ai specific_projects"
PROFILE_DEPS["opencode"]="conda node webdev opencode_agent rtk_ai specific_projects"
PROFILE_DEPS["llama"]="llama_cpp gpu"
PROFILE_DEPS["llama-server"]="llama_cpp gpu"
PROFILE_DEPS["llama-bench"]="llama_cpp gpu"
PROFILE_DEPS["llama-quantize"]="llama_cpp gpu"
PROFILE_DEPS["bash"]="simple_module"
PROFILE_DEPS["rtk"]="rtk_ai"
PROFILE_DEPS["devin"]="conda node webdev devin_cli rtk_ai specific_projects"

# ** Root (identity) module per binary — drives the sandbox prompt color.
# ** If a binary is not listed here, the FIRST module in its PROFILE_DEPS is used.
PROFILE_ROOT_MOD["pi"]="pi_agent"
PROFILE_ROOT_MOD["opencode"]="opencode_agent"
PROFILE_ROOT_MOD["devin"]="devin_cli"

# ==============================================================================
# ENVIRONMENT PRE-LOADING RULES
# ==============================================================================
# ** App-specific setup performed before composition. Runs with TARGET_BIN set.
# ** Smart context fallback: 'pi' defaults to the mandatory "pdf" Conda env when
# ** the host did not pass a custom CONDA_ENV.
if [ "$TARGET_BIN" = "pi" ] && [ -z "${CONDA_ENV:-}" ]; then
  export CONDA_ENV="pdf"
fi
