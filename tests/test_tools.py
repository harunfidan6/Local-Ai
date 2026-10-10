from pathlib import Path

import pytest

from localai.providers.base import ToolCall
from localai.tools import SPECS_BY_NAME, ToolBox, validate_args


@pytest.fixture
def box(tmp_path: Path) -> ToolBox:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def hello():\n    return 'merhaba'\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("gizli", encoding="utf-8")
    return ToolBox(tmp_path, confirm=lambda tool, summary: True)


def run(box: ToolBox, name: str, **args):
    return box.execute(ToolCall(id="1", name=name, arguments=args))


def test_read_file_shows_line_numbers(box):
    result = run(box, "read_file", path="src/app.py")
    assert not result.is_error
    assert "1| def hello():" in result.content


def test_paths_outside_workspace_are_rejected(box):
    result = run(box, "read_file", path="../../etc/passwd")
    assert result.is_error
    assert "dışında" in result.content


def test_list_files_skips_git(box):
    result = run(box, "list_files")
    assert "src/" in result.content
    assert ".git" not in result.content


def test_search_finds_matches(box):
    result = run(box, "search", pattern=r"merhaba", glob="*.py")
    assert "src/app.py:2:" in result.content


def test_edit_file_requires_unique_match(box, tmp_path):
    (tmp_path / "dup.txt").write_text("a\na\n", encoding="utf-8")
    result = run(box, "edit_file", path="dup.txt", old_text="a", new_text="b")
    assert result.is_error and "2 kez" in result.content

    result = run(box, "edit_file", path="src/app.py", old_text="'merhaba'", new_text="'selam'")
    assert not result.is_error
    assert "'selam'" in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")


def test_write_file_creates_directories(box, tmp_path):
    result = run(box, "write_file", path="yeni/klasor/not.md", content="# Not\n")
    assert not result.is_error
    assert (tmp_path / "yeni" / "klasor" / "not.md").read_text(encoding="utf-8") == "# Not\n"


def test_rejected_approval_does_not_write(tmp_path):
    box = ToolBox(tmp_path, confirm=lambda tool, summary: False)
    result = run(box, "write_file", path="x.txt", content="x")
    assert result.is_error
    assert not (tmp_path / "x.txt").exists()


def test_run_command_returns_exit_code(box):
    result = run(box, "run_command", command="python -c \"print('ok')\"")
    assert "[çıkış kodu: 0]" in result.content
    assert "ok" in result.content


def test_invalid_arguments_are_not_executed(box, tmp_path):
    result = box.execute(ToolCall(id="1", name="write_file", arguments={"path": "x.txt"}))
    assert result.is_error and "content" in result.content
    assert not (tmp_path / "x.txt").exists()


def test_integer_strings_are_coerced(box):
    result = run(box, "read_file", path="src/app.py", start_line="2", end_line="2")
    assert not result.is_error
    assert "return 'merhaba'" in result.content
    assert "def hello" not in result.content


def test_validate_args_rejects_bool_for_integer():
    assert validate_args(SPECS_BY_NAME["list_files"], {"max_depth": True})


def test_long_output_is_truncated(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 50_000, encoding="utf-8")
    box = ToolBox(tmp_path, confirm=lambda t, s: True, max_output_chars=1000)
    result = run(box, "read_file", path="big.txt")
    assert "karakter kesildi" in result.content
    assert len(result.content) < 1200


def test_approval_shows_diff(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    seen = []
    box = ToolBox(tmp_path, confirm=lambda tool, summary: seen.append(summary) or True)
    box.execute(ToolCall("1", "edit_file", {"path": "a.py", "old_text": "y = 2", "new_text": "y = 3"}))
    assert "-y = 2" in seen[0] and "+y = 3" in seen[0]


def test_bad_edit_fails_before_asking(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    asked = []
    box = ToolBox(tmp_path, confirm=lambda tool, summary: asked.append(tool) or True)
    result = box.execute(ToolCall("1", "edit_file", {"path": "a.py", "old_text": "yok", "new_text": "z"}))
    assert result.is_error and not asked


def test_undo_restores_edits_and_removes_new_files(box, tmp_path):
    box.begin_task()
    run(box, "edit_file", path="src/app.py", old_text="'merhaba'", new_text="'selam'")
    run(box, "edit_file", path="src/app.py", old_text="'selam'", new_text="'hey'")
    run(box, "write_file", path="yeni.txt", content="x")
    assert box.undo() == ["src/app.py", "yeni.txt"]
    assert "'merhaba'" in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")
    assert not (tmp_path / "yeni.txt").exists()


def test_crlf_files_keep_their_line_endings(box, tmp_path):
    (tmp_path / "win.txt").write_bytes(b"a\r\nb\r\n")
    run(box, "edit_file", path="win.txt", old_text="b", new_text="c")
    assert (tmp_path / "win.txt").read_bytes() == b"a\r\nc\r\n"
    run(box, "write_file", path="lf.txt", content="a\r\nb\n")
    assert (tmp_path / "lf.txt").read_bytes() == b"a\nb\n"
