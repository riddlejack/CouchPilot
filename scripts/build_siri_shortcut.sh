#!/bin/zsh

set -euo pipefail

readonly script_dir="${0:A:h}"
readonly repo_root="${script_dir:h}"
readonly source_file="$repo_root/shortcuts/Home Media.cherri"
readonly default_output="$repo_root/dist/Home Media.shortcut"
readonly required_version="v2.3.0"
readonly default_broker_url="http://home-media.local:8744/v1/intent"

mode="check"
output_file="$default_output"
broker_url="${HOME_MEDIA_BROKER_URL:-$default_broker_url}"

usage() {
  print -u2 -- "usage: ${0:t} [--check | --sign] [--output PATH] [--broker-url URL]"
}

while (( $# > 0 )); do
  case "$1" in
    --check)
      mode="check"
      ;;
    --sign)
      mode="sign"
      ;;
    --output)
      shift
      (( $# > 0 )) || { usage; exit 64; }
      output_file="$1"
      ;;
    --broker-url)
      shift
      (( $# > 0 )) || { usage; exit 64; }
      broker_url="$1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 64
      ;;
  esac
  shift
done

cherri_bin="${CHERRI_BIN:-$(command -v cherri || true)}"
if [[ -z "$cherri_bin" || ! -x "$cherri_bin" ]]; then
  print -u2 -- "Cherri $required_version is required. Install it with:"
  print -u2 -- "  brew install electrikmilk/cherri/cherri"
  exit 69
fi

version_output="$($cherri_bin --version --no-ansi 2>&1)"
if [[ "$version_output" != *"$required_version"* ]]; then
  print -u2 -- "Expected Cherri $required_version; got: $version_output"
  exit 69
fi

mkdir -p "${output_file:h}"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/home-media-shortcut.XXXXXX")"
work_file="$work_dir/Home Media.cherri"
unsigned_file="$work_dir/Home Media_unsigned.shortcut"
trap 'rm -f -- "$work_file" "$unsigned_file"; rmdir -- "$work_dir" 2>/dev/null || true' EXIT
cp "$source_file" "$work_file"

python3 - "$work_file" "$default_broker_url" "$broker_url" <<'PY'
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

path = Path(sys.argv[1])
default_url = sys.argv[2]
broker_url = sys.argv[3]
parsed = urlsplit(broker_url)
try:
    parsed.port
except ValueError as exc:
    raise SystemExit(f"invalid broker URL port: {exc}") from exc
if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname) is None
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path != "/v1/intent"
    or any(character.isspace() or character in {'"', "\\"} for character in broker_url)
):
    raise SystemExit("broker URL must be an HTTP(S) origin plus /v1/intent without credentials, query, or fragment")
source = path.read_text(encoding="utf-8")
if source.count(default_url) != 1:
    raise SystemExit("generic broker URL must appear exactly once in Shortcut source")
path.write_text(source.replace(default_url, broker_url), encoding="utf-8")
PY

if [[ "$mode" == "sign" ]]; then
  # Cherri delegates signing to Apple's local Shortcuts signing service. Apple
  # receives a copy for validation, matching the Shortcuts export behavior.
  "$cherri_bin" "$work_file" \
    --derive-uuids \
    --share=contacts \
    --output="$output_file" \
    --no-ansi
else
  "$cherri_bin" "$work_file" \
    --derive-uuids \
    --skip-sign \
    --no-ansi
  [[ -s "$unsigned_file" ]] || {
    print -u2 -- "Shortcut compiler produced no unsigned artifact"
    exit 1
  }
  mv "$unsigned_file" "$output_file"
fi

[[ -s "$output_file" ]] || { print -u2 -- "Shortcut build produced no artifact"; exit 1; }

if [[ "$mode" == "check" ]]; then
  plutil -lint "$output_file" >/dev/null
  shortcut_description="$(plutil -p "$output_file")"
  for required_text in \
    "Paste the Home Media broker token" \
    "$broker_url" \
    "is.workflow.actions.downloadurl" \
    "WFNoOutputSurfaceBehavior" \
    "spoken_response"; do
    if [[ "$shortcut_description" != *"$required_text"* ]]; then
      print -u2 -- "Shortcut validation failed: missing $required_text"
      exit 1
    fi
  done
fi

print -- "$output_file"
