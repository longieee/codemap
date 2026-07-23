#!/usr/bin/env bash
# wiki-init.sh — Bootstrap a new LLM Wiki from scratch.
#
# Usage:
#   bash wiki-init.sh \
#     --name "myproduct-llm-wiki" \
#     --product "My Product" \
#     --workspace "/path/to/workspace" \
#     --output "/path/to/myproduct-llm-wiki"
#
# What it does:
#   1. Creates directory structure
#   2. Copies schema templates from templates/_schema/
#   3. Scans --workspace for source repos
#   4. Generates _config/sources.yaml
#   5. Generates _config/enrich-manifest.json with current git HEADs
#   6. Creates index.md and DESIGN.md from templates
#   7. Initializes git repo

set -euo pipefail

# Resolve the directory where this script lives (for finding templates/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/templates"

# --- Parse arguments ---
WIKI_NAME=""
PRODUCT=""
WORKSPACE=""
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --name)      WIKI_NAME="$2"; shift 2 ;;
        --product)   PRODUCT="$2"; shift 2 ;;
        --workspace) WORKSPACE="$2"; shift 2 ;;
        --output)    OUTPUT="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: bash wiki-init.sh --name <wiki-name> --product <product-name> --workspace <path> --output <path>"
            echo ""
            echo "Arguments:"
            echo "  --name       Wiki repo directory name (e.g. myproduct-llm-wiki)"
            echo "  --product    Human-readable product name (e.g. My Product)"
            echo "  --workspace  Path to workspace containing source repos to scan"
            echo "  --output     Path where the wiki repo will be created"
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -z "$WIKI_NAME" || -z "$PRODUCT" || -z "$WORKSPACE" || -z "$OUTPUT" ]]; then
    echo "Error: --name, --product, --workspace, and --output are all required."
    echo "Run with --help for usage."
    exit 1
fi

if [[ -d "$OUTPUT" && "$(ls -A "$OUTPUT" 2>/dev/null)" ]]; then
    echo "Error: $OUTPUT already exists and is not empty."
    exit 1
fi

if [[ ! -d "$TEMPLATES_DIR" ]]; then
    echo "Error: Templates directory not found at $TEMPLATES_DIR"
    echo "This script expects a templates/ folder next to it."
    exit 1
fi

log() { echo "[wiki-init] $*"; }
TODAY=$(date +%Y-%m-%d)
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- 1. Create directory structure ---
log "Creating directory structure at $OUTPUT..."
mkdir -p "$OUTPUT"/{services,apis,components,data-stores,infrastructure,_schema,_config,_analysis,_reports}

# --- 2. Copy schema templates ---
log "Copying schema templates from $TEMPLATES_DIR/_schema/..."
cp "$TEMPLATES_DIR"/_schema/*.md "$OUTPUT/_schema/"

# --- 3. Scan workspace for source repos ---
log "Scanning workspace for source repos: $WORKSPACE"

declare -a REPOS=()
declare -a REPO_HINTS=()

for dir in "$WORKSPACE"/*/; do
    [[ ! -d "$dir" ]] && continue
    dir_name=$(basename "$dir")

    # Skip hidden dirs, the wiki itself, and non-repo dirs
    [[ "$dir_name" == .* ]] && continue
    [[ "$dir_name" == "$WIKI_NAME" ]] && continue
    [[ "$dir_name" == _* ]] && continue
    [[ "$dir_name" == node_modules ]] && continue

    # Check if this looks like a source repo
    is_repo=false
    hints=""

    if [[ -f "$dir/Dockerfile" || -f "$dir/cloudbuild.yaml" ]]; then
        is_repo=true
        hints="service"
    fi
    if [[ -f "$dir/pyproject.toml" || -f "$dir/package.json" ]]; then
        is_repo=true
        [[ -z "$hints" ]] && hints="component"
    fi
    if [[ -f "$dir/Makefile" ]]; then
        is_repo=true
    fi

    if $is_repo; then
        # Detect API patterns
        if grep -rql '@mcp.tool\|FastMCP\|mcp_server' "$dir"/*.py "$dir"/src/*.py 2>/dev/null; then
            hints="$hints, api"
        elif grep -rql 'FastAPI\|flask\|express' "$dir"/*.py "$dir"/src/*.py "$dir"/*.ts 2>/dev/null; then
            hints="$hints, api"
        fi

        # Detect data store patterns
        if grep -rql 'mongodb\|MongoClient\|pymongo\|motor' "$dir"/*.py "$dir"/src/*.py "$dir"/config/*.yaml 2>/dev/null; then
            hints="$hints, data-store"
        elif grep -rql 'bigquery\|BigQuery\|google.cloud.bigquery' "$dir"/*.py "$dir"/src/*.py 2>/dev/null; then
            hints="$hints, data-store"
        fi

        REPOS+=("$dir_name")
        REPO_HINTS+=("$hints")
    fi
done

log "Found ${#REPOS[@]} source repos"

# --- 4. Generate sources.yaml ---
log "Generating _config/sources.yaml..."

# Start from template header
sed "s/{{PRODUCT}}/$PRODUCT/g" "$TEMPLATES_DIR/sources.yaml.tmpl" > "$OUTPUT/_config/sources.yaml"

for i in "${!REPOS[@]}"; do
    repo="${REPOS[$i]}"
    hint="${REPO_HINTS[$i]}"
    hint_yaml=$(echo "$hint" | sed 's/^/[ /; s/$/ ]/; s/, /, /g')
    cat >> "$OUTPUT/_config/sources.yaml" << EOF
  - name: $repo
    type: local_repo
    path: $WORKSPACE/$repo
    hints: $hint_yaml

EOF
done

# --- 5. Generate enrich-manifest.json ---
log "Generating _config/enrich-manifest.json..."

manifest="{
  \"product\": \"$PRODUCT\",
  \"wiki_repo\": \"$WIKI_NAME\",
  \"created\": \"$NOW\",
  \"repos\": {"

first=true
for repo in "${REPOS[@]}"; do
    repo_path="$WORKSPACE/$repo"
    commit="unknown"
    if [[ -d "$repo_path/.git" ]]; then
        commit=$(git -C "$repo_path" rev-parse HEAD 2>/dev/null || echo "unknown")
    fi

    $first && first=false || manifest+=","
    manifest+="
    \"$repo\": {
      \"last_commit\": \"$commit\",
      \"last_sync\": \"$NOW\",
      \"sync_count\": 0,
      \"pages\": []
    }"
done

manifest+="
  }
}"

echo "$manifest" > "$OUTPUT/_config/enrich-manifest.json"

# Pretty-print if python3 is available
if command -v python3 &>/dev/null; then
    python3 -m json.tool "$OUTPUT/_config/enrich-manifest.json" > "$OUTPUT/_config/enrich-manifest.json.tmp" \
        && mv "$OUTPUT/_config/enrich-manifest.json.tmp" "$OUTPUT/_config/enrich-manifest.json"
fi

# --- 6. Create index.md and DESIGN.md from templates ---
log "Creating index.md..."

# Build repo list for DESIGN.md
repo_list=""
for repo in "${REPOS[@]}"; do
    repo_list+="- \`$repo\`
"
done

# index.md — substitute placeholders + append repo list
sed -e "s/{{PRODUCT}}/$PRODUCT/g" -e "s/{{TODAY}}/$TODAY/g" "$TEMPLATES_DIR/index.md.tmpl" > "$OUTPUT/index.md"

# Append discovered repos section
cat >> "$OUTPUT/index.md" << EOF

---

## Source Repos (${#REPOS[@]} discovered)

$repo_list
EOF

log "Creating DESIGN.md..."

# DESIGN.md — use Python for reliable multiline template substitution
python3 -c "
import sys
tmpl = open('$TEMPLATES_DIR/DESIGN.md.tmpl').read()
repo_list = sys.stdin.read()
out = tmpl.replace('{{PRODUCT}}', '$PRODUCT').replace('{{TODAY}}', '$TODAY').replace('{{REPO_LIST}}', repo_list.strip())
open('$OUTPUT/DESIGN.md', 'w').write(out)
" <<< "$repo_list"

# --- 7. Initialize git ---
log "Initializing git repo..."
cd "$OUTPUT"
git init -q
git add -A
git commit -q -m "feat: bootstrap LLM wiki for $PRODUCT"

# --- Done ---
echo ""
log "=== Wiki bootstrapped successfully ==="
log "Location: $OUTPUT"
log "Repos discovered: ${#REPOS[@]}"
log ""
log "Next steps:"
log "  1. Review _config/sources.yaml — adjust hints, remove false positives"
log "  2. Review _config/enrich-manifest.json — verify commit hashes"
log "  3. Cold-start the pages via the orchestration loop (llm-wiki-init SKILL §3a):"
log "     survey -> page-inventory backbone (init/inventory.py; coverage.gaps must be [])"
log "     -> bounded per-page workers -> evaluator-optimizer verify loop."
log "     Then delta-enrich via the llm-wiki-maintainer skill."
log "  4. Deploy MCP server (see llm-wiki-init skill Section 5)"
log "  5. Push: git remote add origin <url> && git push -u origin main"
