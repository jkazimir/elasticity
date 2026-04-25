"""Tests for elasticity.tools.filesystem (read, write, list_dir, grep, edit)."""

import pytest
from pathlib import Path

from elasticity.tools.filesystem import read, write, list_dir, grep, edit, glob, delete, move


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_tree(tmp_path: Path) -> Path:
    """Create a small directory tree for tests:

    tmp_path/
      hello.py        — contains TODO and a function
      world.txt       — plain text, no TODO
      sub/
        nested.py     — contains TODO
      binary.bin      — binary file (null bytes, should be skipped)
      .git/
        config        — should be skipped by grep
    """
    (tmp_path / "hello.py").write_text(
        "# TODO: fix this\ndef hello():\n    return 'hello'\n", encoding="utf-8"
    )
    (tmp_path / "world.txt").write_text("hello world\n", encoding="utf-8")

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.py").write_text(
        "# TODO: nested task\nclass Nested:\n    pass\n", encoding="utf-8"
    )

    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02binary data")

    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("# TODO inside .git\n", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# grep — basic matching
# ---------------------------------------------------------------------------

class TestGrep:
    def test_finds_pattern_recursively(self, tmp_tree: Path) -> None:
        result = grep("TODO", path=str(tmp_tree))
        assert "hello.py" in result
        assert "nested.py" in result

    def test_no_matches_returns_message(self, tmp_tree: Path) -> None:
        result = grep("NONEXISTENT_PATTERN_XYZ", path=str(tmp_tree))
        assert "No matches" in result

    def test_glob_filter_limits_files(self, tmp_tree: Path) -> None:
        result = grep("hello", path=str(tmp_tree), glob="*.py")
        # hello.py contains "hello" in return statement; world.txt also has "hello world"
        # but with *.py glob, world.txt should be excluded
        assert "world.txt" not in result

    def test_glob_filter_matches_txt(self, tmp_tree: Path) -> None:
        result = grep("hello", path=str(tmp_tree), glob="*.txt")
        assert "world.txt" in result
        assert "hello.py" not in result

    def test_skips_binary_files(self, tmp_tree: Path) -> None:
        # binary.bin has null bytes — grep should not include it
        result = grep("binary", path=str(tmp_tree))
        assert "binary.bin" not in result

    def test_skips_git_directory(self, tmp_tree: Path) -> None:
        result = grep("TODO", path=str(tmp_tree))
        assert ".git" not in result

    def test_format_is_file_lineno_content(self, tmp_tree: Path) -> None:
        result = grep("TODO", path=str(tmp_tree / "hello.py"))
        # Should be: path:1:# TODO: fix this
        assert ":1:" in result
        assert "TODO: fix this" in result

    def test_regex_pattern(self, tmp_tree: Path) -> None:
        # Match lines starting with "def " or "class "
        result = grep(r"^(def|class) ", path=str(tmp_tree))
        assert "def hello" in result
        assert "class Nested" in result

    def test_invalid_regex_raises(self, tmp_tree: Path) -> None:
        with pytest.raises(ValueError, match="Invalid regex"):
            grep("[unclosed", path=str(tmp_tree))

    def test_nonexistent_path_raises(self, tmp_tree: Path) -> None:
        with pytest.raises(FileNotFoundError):
            grep("TODO", path=str(tmp_tree / "does_not_exist"))

    def test_search_single_file(self, tmp_tree: Path) -> None:
        result = grep("return", path=str(tmp_tree / "hello.py"))
        assert "return 'hello'" in result

    def test_truncation_at_two_hundred_matches(self, tmp_path: Path) -> None:
        # Create a file with 210 matching lines
        lines = "\n".join(f"match line {i}" for i in range(210))
        (tmp_path / "many.txt").write_text(lines, encoding="utf-8")
        result = grep("match line", path=str(tmp_path))
        assert "truncated at 200" in result
        # Exactly 200 match lines should appear
        assert result.count("match line") == 200


# ---------------------------------------------------------------------------
# read — basic and line-range
# ---------------------------------------------------------------------------

class TestRead:
    def test_read_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("content", encoding="utf-8")
        assert read(str(f)) == "content"

    def test_read_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read(str(tmp_path / "missing.txt"))

    def test_read_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            read(str(tmp_path))

    def test_read_full_file_unchanged(self, tmp_path: Path) -> None:
        content = "line1\nline2\nline3\n"
        f = tmp_path / "f.txt"
        f.write_text(content, encoding="utf-8")
        # Both 0s → full content, no line-number prefixes
        assert read(str(f), 0, 0) == content

    def test_read_range_returns_numbered_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "r.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
        result = read(str(f), start_line=2, end_line=3)
        assert "2\t" in result
        assert "3\t" in result
        assert "beta" in result
        assert "gamma" in result
        assert "alpha" not in result
        assert "delta" not in result

    def test_read_range_single_line(self, tmp_path: Path) -> None:
        f = tmp_path / "s.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        result = read(str(f), start_line=2, end_line=2)
        assert "2\t" in result
        assert "b" in result
        assert "a" not in result
        assert "c" not in result

    def test_read_range_clamped_beyond_end(self, tmp_path: Path) -> None:
        f = tmp_path / "c.txt"
        f.write_text("x\ny\nz\n", encoding="utf-8")
        # end_line beyond file length should clamp to last line
        result = read(str(f), start_line=2, end_line=999)
        assert "y" in result
        assert "z" in result
        assert "x" not in result

    def test_read_range_start_only(self, tmp_path: Path) -> None:
        f = tmp_path / "so.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        # end_line=0 means "to end of file"
        result = read(str(f), start_line=2, end_line=0)
        assert "b" in result
        assert "c" in result
        assert "a" not in result

    def test_read_range_invalid_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "inv.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid range"):
            read(str(f), start_line=5, end_line=2)


# ---------------------------------------------------------------------------
# write / list_dir — basic smoke tests
# ---------------------------------------------------------------------------

class TestWrite:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        f = tmp_path / "out.txt"
        result = write(str(f), "hello")
        assert f.read_text(encoding="utf-8") == "hello"
        assert "Successfully wrote" in result

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        f = tmp_path / "a" / "b" / "c.txt"
        write(str(f), "deep")
        assert f.read_text(encoding="utf-8") == "deep"

    def test_write_overwrites_existing(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("old", encoding="utf-8")
        write(str(f), "new")
        assert f.read_text(encoding="utf-8") == "new"


class TestListDir:
    def test_lists_files_and_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub").mkdir()
        result = list_dir(str(tmp_path))
        assert "FILE\ta.txt" in result
        assert "DIR\tsub" in result

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert list_dir(str(tmp_path)) == "(empty directory)"

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            list_dir(str(tmp_path / "nope"))

    def test_file_path_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "f.txt"
        f.write_text("x")
        with pytest.raises(NotADirectoryError):
            list_dir(str(f))


# ---------------------------------------------------------------------------
# edit — targeted string replacement
# ---------------------------------------------------------------------------

class TestEdit:
    def test_happy_path_replaces_string(self, tmp_path: Path) -> None:
        f = tmp_path / "code.py"
        f.write_text("def old_name():\n    pass\n", encoding="utf-8")
        result = edit(str(f), "old_name", "new_name")
        assert f.read_text(encoding="utf-8") == "def new_name():\n    pass\n"
        assert "Replaced" in result

    def test_reports_line_number(self, tmp_path: Path) -> None:
        f = tmp_path / "lines.py"
        f.write_text("line1\nline2\nTARGET\nline4\n", encoding="utf-8")
        result = edit(str(f), "TARGET", "REPLACED")
        assert "line 3" in result

    def test_preserves_rest_of_file(self, tmp_path: Path) -> None:
        original = "header\nFIND ME\nfooter\n"
        f = tmp_path / "p.txt"
        f.write_text(original, encoding="utf-8")
        edit(str(f), "FIND ME", "DONE")
        content = f.read_text(encoding="utf-8")
        assert content == "header\nDONE\nfooter\n"

    def test_not_found_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "nf.py"
        f.write_text("hello world\n", encoding="utf-8")
        with pytest.raises(ValueError, match="not found"):
            edit(str(f), "missing string", "replacement")

    def test_ambiguous_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "dup.py"
        f.write_text("foo\nfoo\n", encoding="utf-8")
        with pytest.raises(ValueError, match="2 times"):
            edit(str(f), "foo", "bar")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            edit(str(tmp_path / "ghost.py"), "x", "y")

    def test_multiline_old_string(self, tmp_path: Path) -> None:
        f = tmp_path / "ml.py"
        f.write_text("def foo():\n    return 1\n\ndef bar():\n    pass\n", encoding="utf-8")
        edit(str(f), "def foo():\n    return 1", "def foo():\n    return 42")
        content = f.read_text(encoding="utf-8")
        assert "return 42" in content
        assert "return 1" not in content


# ---------------------------------------------------------------------------
# glob — pattern-based file discovery
# ---------------------------------------------------------------------------

class TestGlob:
    def test_matches_py_files_recursively(self, tmp_tree: Path) -> None:
        result = glob("**/*.py", path=str(tmp_tree))
        assert "hello.py" in result
        assert "nested.py" in result

    def test_no_matches_returns_message(self, tmp_tree: Path) -> None:
        result = glob("**/*.nonexistent", path=str(tmp_tree))
        assert "No files matching" in result

    def test_skips_git_directory(self, tmp_tree: Path) -> None:
        result = glob("**/*", path=str(tmp_tree))
        assert ".git" not in result

    def test_base_path_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            glob("**/*.py", path=str(tmp_path / "does_not_exist"))

    def test_pattern_in_subdirectory(self, tmp_tree: Path) -> None:
        result = glob("sub/*.py", path=str(tmp_tree))
        assert "nested.py" in result
        assert "hello.py" not in result

    def test_truncation_at_limit(self, tmp_path: Path) -> None:
        # Create 501 .txt files
        for i in range(501):
            (tmp_path / f"file_{i:04d}.txt").write_text("x", encoding="utf-8")
        result = glob("*.txt", path=str(tmp_path))
        assert "truncated at 500" in result


# ---------------------------------------------------------------------------
# delete — file and directory removal
# ---------------------------------------------------------------------------

class TestDelete:
    def test_deletes_file(self, tmp_path: Path) -> None:
        f = tmp_path / "todelete.txt"
        f.write_text("bye", encoding="utf-8")
        result = delete(str(f))
        assert not f.exists()
        assert "Deleted" in result

    def test_deletes_empty_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "emptydir"
        d.mkdir()
        result = delete(str(d))
        assert not d.exists()
        assert "Deleted" in result

    def test_rejects_non_empty_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "nonempty"
        d.mkdir()
        (d / "file.txt").write_text("content", encoding="utf-8")
        with pytest.raises(ValueError, match="non-empty"):
            delete(str(d))

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            delete(str(tmp_path / "ghost.txt"))


# ---------------------------------------------------------------------------
# move — rename and relocate files/directories
# ---------------------------------------------------------------------------

class TestMove:
    def test_renames_file(self, tmp_path: Path) -> None:
        src = tmp_path / "original.txt"
        src.write_text("hello", encoding="utf-8")
        dst = tmp_path / "renamed.txt"
        result = move(str(src), str(dst))
        assert not src.exists()
        assert dst.read_text(encoding="utf-8") == "hello"
        assert "Moved" in result

    def test_moves_to_new_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        src.write_text("content", encoding="utf-8")
        dst = tmp_path / "newdir" / "subdir" / "file.txt"
        move(str(src), str(dst))
        assert not src.exists()
        assert dst.read_text(encoding="utf-8") == "content"

    def test_moves_directory(self, tmp_path: Path) -> None:
        src = tmp_path / "srcdir"
        src.mkdir()
        (src / "inner.txt").write_text("data", encoding="utf-8")
        dst = tmp_path / "dstdir"
        move(str(src), str(dst))
        assert not src.exists()
        assert (dst / "inner.txt").read_text(encoding="utf-8") == "data"

    def test_missing_source_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            move(str(tmp_path / "nope.txt"), str(tmp_path / "dest.txt"))
