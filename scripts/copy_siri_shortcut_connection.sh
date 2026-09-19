#!/bin/zsh

set -euo pipefail

readonly token_file="${HOME_MEDIA_BROKER_TOKEN_FILE:-$HOME/.config/home-media/broker-token}"

if [[ ! -f "$token_file" || -L "$token_file" ]]; then
  print -u2 -- "Broker token must be a regular, non-symlink file: $token_file"
  exit 66
fi

mode="$(stat -f '%Lp' "$token_file")"
owner="$(stat -f '%u' "$token_file")"
if [[ "$mode" != "600" && "$mode" != "400" ]]; then
  print -u2 -- "Broker token permissions must be 600 or 400"
  exit 77
fi
if [[ "$owner" != "$EUID" ]]; then
  print -u2 -- "Broker token must be owned by the current user"
  exit 77
fi

token="$(<"$token_file")"
if [[ ! "$token" =~ '^[0-9A-Fa-f]{32,}$' ]]; then
  print -u2 -- "Broker token must be a single hexadecimal value of at least 32 characters"
  exit 65
fi
printf %s "$token" | /usr/bin/pbcopy
token=""
print -- "Home Media broker token copied to the clipboard. Paste it into the import prompt."
