#!/bin/sh

set -eu

VERSION="${TODO_CLI_VERSION:-v1.3.0}"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/todo-cli"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DOWNLOAD_URL="https://raw.githubusercontent.com/veenaskumar-cisco/todo-cli/${VERSION}/todo.py"
EXPECTED_SHA256="dd81a75e67895320edcde99c0a2e8da67736cac3f1530f864421167bddf40ddf"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 is required but was not found." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is required but was not found." >&2
    exit 1
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR"
temporary_file="$(mktemp)"
trap 'rm -f "$temporary_file"' EXIT HUP INT TERM

echo "Downloading Daily Todo CLI ${VERSION}..."
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    "$DOWNLOAD_URL" --output "$temporary_file"

actual_sha256="$(
    python3 - "$temporary_file" <<'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"

if [ "$actual_sha256" != "$EXPECTED_SHA256" ]; then
    echo "Error: the downloaded file failed its integrity check." >&2
    exit 1
fi

python3 -m py_compile "$temporary_file"
install -m 0644 "$temporary_file" "$INSTALL_DIR/todo.py"

cat >"$BIN_DIR/todo" <<'EOF'
#!/bin/sh
exec python3 "${XDG_DATA_HOME:-$HOME/.local/share}/todo-cli/todo.py" "$@"
EOF
chmod 0755 "$BIN_DIR/todo"

echo "Installed todo at $BIN_DIR/todo"

case ":$PATH:" in
    *":$BIN_DIR:"*)
        echo "Run: todo --help"
        ;;
    *)
        echo "Add this line to your shell configuration, then restart the shell:"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
        echo "After that, run: todo --help"
        ;;
esac
