"""file_listing 与 list_files glob/depth 测试。"""

from agent_runtime.file_listing import list_directory_entries
from agent_runtime.tool_context import ToolContext
from agent_runtime.tools import IGNORED_PATH_NAMES, tool_list_files


class TestFileListing:
    def test_shallow_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "src").mkdir()
        lines, total = list_directory_entries(tmp_path, depth=1, ignored_names=IGNORED_PATH_NAMES)
        assert total == 2
        assert "[F] a.py" in lines
        assert "[D] src" in lines

    def test_shallow_glob_py(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        lines, total = list_directory_entries(
            tmp_path, depth=1, glob_pattern="*.py", ignored_names=IGNORED_PATH_NAMES
        )
        assert total == 1
        assert lines == ["[F] a.py"]

    def test_recursive_depth_two(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "calc.py").write_text("1")
        (tmp_path / "root.py").write_text("2")
        lines, total = list_directory_entries(tmp_path, depth=2, ignored_names=IGNORED_PATH_NAMES)
        assert total == 2
        assert "[F] root.py" in lines
        assert "[F] src/calc.py" in lines
        assert not any(line.startswith("[D]") for line in lines)

    def test_recursive_glob_py(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "calc.py").write_text("1")
        (src / "note.txt").write_text("n")
        lines, total = list_directory_entries(
            tmp_path, depth=2, glob_pattern="*.py", ignored_names=IGNORED_PATH_NAMES
        )
        assert total == 1
        assert lines == ["[F] src/calc.py"]

    def test_max_results_truncation(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text("x")
        lines, total = list_directory_entries(
            tmp_path,
            depth=1,
            glob_pattern="*.py",
            max_results=2,
            ignored_names=IGNORED_PATH_NAMES,
        )
        assert len(lines) == 2
        assert total == 5

    def test_skips_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x")
        (tmp_path / "ok.py").write_text("1")
        lines, total = list_directory_entries(tmp_path, depth=3, ignored_names=IGNORED_PATH_NAMES)
        assert total == 1
        assert lines == ["[F] ok.py"]


class TestListFilesTool:
    def test_depth_two_via_tool(self, temp_workspace):
        src = temp_workspace / "pkg"
        src.mkdir()
        (src / "mod.py").write_text("x")
        ctx = ToolContext(root=str(temp_workspace))
        result = tool_list_files(ctx, {"path": ".", "depth": 2})
        assert "[F] pkg/mod.py" in result

    def test_glob_no_match_message(self, temp_workspace):
        ctx = ToolContext(root=str(temp_workspace))
        result = tool_list_files(ctx, {"path": ".", "glob": "*.xyz"})
        assert "(无匹配)" in result
