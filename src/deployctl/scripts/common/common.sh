#!/usr/bin/env bash
##############################################################################
# Print helpers and the dry-run wrapper for the deployctl bash core.
#
# These scripts are invoked by the Python CLI with a fully-resolved environment
# — they parse no configuration of their own. The output markers match cli/ui.py
# so CLI output and streamed script output read as one log.
##############################################################################

# Colors only on a terminal; the control panel strips ANSI anyway.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    readonly RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m' BLUE='\033[0;34m' NC='\033[0m'
else
    readonly RED='' GREEN='' YELLOW='' BLUE='' NC=''
fi

print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_separator() { echo -e "${BLUE}────────────────────────────────────────${NC}"; }
print_header() {
    echo -e "${BLUE}╔════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║  $1${NC}"
    echo -e "${BLUE}╚════════════════════════════════════════╝${NC}"
    echo ""
}

# Confirm with the operator. ASSUME_YES=1 (set by the CLI and the panel)
# auto-confirms so nothing ever blocks on a prompt in non-interactive use.
confirm_action() {
    local prompt="$1"
    if [[ "${ASSUME_YES:-}" == "1" || "${ASSUME_YES:-}" == "true" ]]; then
        return 0
    fi
    read -r -p "$(echo -e "${YELLOW}${prompt} [y/N]: ${NC}")" response
    [[ "$response" =~ ^[yY]([eE][sS])?$ ]]
}

# Dry-run wrapper. Under DEPLOYCTL_DRY_RUN=1 every mutating command is echoed
# instead of executed, so `deploy update --dry-run` prints the exact ssh/rsync/
# compose invocations a real run would perform.
maybe() {
    if [[ "${DEPLOYCTL_DRY_RUN:-}" == "1" ]]; then
        echo "  [dry-run] $*"
        return 0
    fi
    "$@"
}

command_exists() { command -v "$1" &>/dev/null; }

# Octal permissions of a local path. `stat` is not portable — GNU takes -c, BSD
# takes -f — and the control machine is frequently a Mac, so both have to work.
# (Remote hosts are always Linux, so the ssh side can assume GNU.)
file_mode() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null
}

# Whether any uid can read a local path. This is the question that matters for a
# file bind-mounted into a container: the process inside runs under an
# image-chosen uid with no relationship to the file's owner on the host.
world_readable() {
    local mode
    mode="$(file_mode "$1")" || return 1
    # Last octal digit is the "other" triad; bit 4 is read.
    (( (10#${mode: -1} & 4) != 0 ))
}
