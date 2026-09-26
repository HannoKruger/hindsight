"""read_transcript: which transcript entries become retained messages."""

import importlib.util
import json
import os
import sys


def load_retain_module():
    scripts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
    sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("retain_under_test", os.path.join(scripts_dir, "retain.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_transcript(tmp_path, entries):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries))
    return str(transcript)


def nested_entry(role, content, **extra):
    return {"type": role, "message": {"role": role, "content": content}, **extra}


def test_keeps_real_user_and_assistant_messages(tmp_path):
    retain = load_retain_module()
    transcript = write_transcript(tmp_path, [
        nested_entry("user", "deploy the app"),
        nested_entry("assistant", "deployed"),
    ])

    messages = retain.read_transcript(transcript)

    assert [message["content"] for message in messages] == ["deploy the app", "deployed"]


def test_skips_meta_entries_such_as_loaded_skill_text(tmp_path):
    retain = load_retain_module()
    transcript = write_transcript(tmp_path, [
        nested_entry("user", "check titan"),
        nested_entry("user", "Base directory for this skill: ... full SKILL.md ...", isMeta=True),
        nested_entry("assistant", "titan is up"),
    ])

    messages = retain.read_transcript(transcript)

    assert [message["content"] for message in messages] == ["check titan", "titan is up"]
