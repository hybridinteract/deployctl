"""Env-file parsing — the edge cases that bite in real config files."""

from __future__ import annotations

from deployctl.cli.envfile import parse_env_text, patch_env_file, quote_value


def test_basic_assignment():
    assert parse_env_text("A=1\nB=two") == {"A": "1", "B": "two"}


def test_empty_value():
    # Regression: a naive `value[:1] in "\"'"` check is True for "" — every string
    # contains the empty string — and then indexing value[0] raises.
    assert parse_env_text("EMPTY=\nAFTER=1") == {"EMPTY": "", "AFTER": "1"}


def test_export_prefix_is_ignored():
    assert parse_env_text("export TOKEN=abc") == {"TOKEN": "abc"}


def test_quotes_are_stripped():
    assert parse_env_text('HOSTS="10.0.0.1 10.0.0.2"') == {"HOSTS": "10.0.0.1 10.0.0.2"}
    assert parse_env_text("NAME='value'") == {"NAME": "value"}


def test_hash_inside_a_password_is_kept():
    # A '#' with no preceding whitespace is part of the value, as in bash.
    assert parse_env_text("PW=abc#def") == {"PW": "abc#def"}


def test_trailing_comment_is_dropped():
    assert parse_env_text("PORT=5432 # the default") == {"PORT": "5432"}


def test_comments_and_blank_lines_are_skipped():
    assert parse_env_text("# note\n\n  \nA=1") == {"A": "1"}


def test_later_assignment_wins():
    assert parse_env_text("A=1\nA=2") == {"A": "2"}


def test_values_containing_equals():
    assert parse_env_text("URL=postgres://u:p@h/db?x=1") == {"URL": "postgres://u:p@h/db?x=1"}


def test_quote_value_roundtrip():
    for raw in ("simple", "with space", "with#hash", "", "quote\"inside"):
        assert parse_env_text(f"K={quote_value(raw)}") == {"K": raw}


def test_patch_rewrites_in_place_and_appends(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# header\nA=old\nB=keep\n")
    patch_env_file(path, {"A": "new", "C": "added"})
    parsed = parse_env_text(path.read_text())
    assert parsed == {"A": "new", "B": "keep", "C": "added"}
    # Comments and untouched lines survive.
    assert "# header" in path.read_text()
