import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "shortcuts" / "Home Media.cherri"
BUILD_SCRIPT = ROOT / "scripts" / "build_siri_shortcut.sh"
CONNECTION_SCRIPT = ROOT / "scripts" / "copy_siri_shortcut_connection.sh"


def test_shortcut_source_matches_broker_contract_without_a_real_secret() -> None:
    source = SOURCE.read_text(encoding="utf-8")

    assert '"schema_version": 1' in source
    assert '"utterance": "{utterance}"' in source
    assert '"idempotency_key": "ios-{timestamp}-{nonce}"' in source
    assert '"Authorization": "Bearer {token}"' in source
    assert 'getValue(responseDictionary, "spoken_response")' in source
    assert 'mustOutput("{spokenResponse}", "{spokenResponse}")' in source
    assert source.count("#question ") == 1
    assert "Paste the Home Media broker token" in source
    assert 'jsonRequest("http://home-media.local:8744/v1/intent"' in source
    assert "getDictionary(brokerConfigText)" not in source
    assert "PASTE-THE-PRIVATE-BROKER-TOKEN" in source


def test_shortcut_builder_is_safe_by_default_and_pins_the_compiler() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'mode="check"' in script
    assert 'required_version="v2.3.0"' in script
    assert "--skip-sign" in script
    assert "--derive-uuids" in script
    assert 'mode" == "sign' in script
    assert 'HOME_MEDIA_BROKER_URL:-$default_broker_url' in script
    assert "--broker-url" in script


def test_connection_helper_never_prints_the_token() -> None:
    script = CONNECTION_SCRIPT.read_text(encoding="utf-8")

    assert "pbcopy" in script
    assert 'printf %s "$token" | /usr/bin/pbcopy' in script
    assert 'print -- "$token"' not in script
    assert "PASTE-THE-PRIVATE-BROKER-TOKEN" not in script


def test_shortcut_builder_injects_explicit_broker_url(tmp_path: Path) -> None:
    fake_cherri = tmp_path / "cherri"
    captured_source = tmp_path / "captured.cherri"
    output = tmp_path / "Home Media.shortcut"
    broker_url = "https://bridge.example.test/v1/intent"
    fake_cherri.write_text(
        """#!/bin/zsh
set -euo pipefail
if [[ "$1" == "--version" ]]; then
  print -- "Cherri v2.3.0"
  exit 0
fi
source_file="$1"
cp "$source_file" "$CAPTURED_SOURCE"
cat > "${source_file%.cherri}_unsigned.shortcut" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><array>
<string>Paste the Home Media broker token</string>
<string>https://bridge.example.test/v1/intent</string>
<string>is.workflow.actions.downloadurl</string>
<string>WFNoOutputSurfaceBehavior</string>
<string>spoken_response</string>
</array></plist>
PLIST
""",
        encoding="utf-8",
    )
    fake_cherri.chmod(0o700)
    env = os.environ | {
        "CHERRI_BIN": str(fake_cherri),
        "CAPTURED_SOURCE": str(captured_source),
    }

    completed = subprocess.run(
        [
            "zsh",
            str(BUILD_SCRIPT),
            "--check",
            "--broker-url",
            broker_url,
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    rendered_source = captured_source.read_text(encoding="utf-8")
    assert broker_url in rendered_source
    assert "http://home-media.local:8744/v1/intent" not in rendered_source

    rejected = subprocess.run(
        [
            "zsh",
            str(BUILD_SCRIPT),
            "--check",
            "--broker-url",
            'http://bridge.example.test"/v1/intent',
            "--output",
            str(tmp_path / "rejected.shortcut"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert rejected.returncode != 0
    assert not (tmp_path / "rejected.shortcut").exists()
