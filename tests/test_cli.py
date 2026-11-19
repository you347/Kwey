"""A5 kwey CLI 单测：五条子命令、--json 信封、path:/tag:/file: 操作符、
write 落盘且索引可查、退出码与 stderr。

CLI 在进程内直接调 main(argv)（CLI 自行 init/reset services，不依赖 services fixture）；
另含一条真实子进程冒烟用例，验证脚本入口与 Windows UTF-8 stdio。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kwey_cli
from app.api import deps

PROJECT_ROOT = Path(kwey_cli.__file__).resolve().parent


@pytest.fixture
def cli_vault(vault_dir: Path):
    """复用 conftest 的结构化临时 vault；CLI 每次运行自管服务生命周期，结束后兜底清理。"""
    yield vault_dir
    deps.reset_services()


def _json_out(capsys) -> dict:
    """解析 stdout 的 {code,data,message} 信封。"""
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["code"] == 0
    assert envelope["message"] == "ok"
    assert "data" in envelope
    return envelope["data"]


# --------------------------------------------------------------------- search
class TestSearch:
    def test_json_envelope_and_shape(self, cli_vault, capsys):
        code = kwey_cli.main(["--vault", str(cli_vault), "search", "计划", "--json"])
        assert code == 0
        data = _json_out(capsys)
        # data 形状与 GET /api/search 一致
        assert set(data.keys()) == {"hits", "total", "elapsed_ms", "mode"}
        assert data["mode"] == "fts"
        paths = [h["path"] for h in data["hits"]]
        assert "notes/读书/2026计划.md" in paths
        assert data["total"] >= 1
        assert all({"path", "title", "score", "snippet", "tags"} <= set(h.keys()) for h in data["hits"])

    def test_operators_path_tag_file(self, cli_vault, capsys):
        # path: 操作符（纯操作符查询，无检索词）
        assert kwey_cli.main(["search", "--vault", str(cli_vault), "path:notes/工作", "--json"]) == 0
        assert [h["path"] for h in _json_out(capsys)["hits"]] == ["notes/工作/会议记录.md"]
        # tag: 操作符（精确层级）
        assert kwey_cli.main(["search", "--vault", str(cli_vault), "tag:读书/2026", "--json"]) == 0
        assert [h["path"] for h in _json_out(capsys)["hits"]] == ["notes/读书/2026计划.md"]
        # file: 操作符
        assert kwey_cli.main(["search", "--vault", str(cli_vault), "file:会议", "--json"]) == 0
        assert [h["path"] for h in _json_out(capsys)["hits"]] == ["notes/工作/会议记录.md"]

    def test_human_output(self, cli_vault, capsys):
        assert kwey_cli.main(["--vault", str(cli_vault), "search", "计划"]) == 0
        out = capsys.readouterr().out
        assert "notes/读书/2026计划.md" in out
        assert "<mark>" not in out  # 人类可读输出剥离高亮标签

    def test_limit_out_of_range_exit_2(self, cli_vault):
        assert kwey_cli.main(["--vault", str(cli_vault), "search", "x", "--limit", "500"]) == 2


# --------------------------------------------------------------------- read
class TestRead:
    def test_json_includes_frontmatter(self, cli_vault, capsys):
        assert kwey_cli.main(["--vault", str(cli_vault), "read", "欢迎.md", "--json"]) == 0
        data = _json_out(capsys)
        assert data["path"] == "欢迎.md"
        assert data["title"] == "欢迎"
        assert data["frontmatter"]["自定义"] == "保留我"
        assert "# 欢迎" in data["content"]
        assert "mtime" in data

    def test_human_prints_body_without_frontmatter(self, cli_vault, capsys):
        assert kwey_cli.main(["--vault", str(cli_vault), "read", "欢迎.md"]) == 0
        out = capsys.readouterr().out
        assert "链接到" in out  # 正文
        assert "title: 欢迎" not in out  # 不含 frontmatter

    def test_missing_note_exit_1(self, cli_vault, capsys):
        assert kwey_cli.main(["--vault", str(cli_vault), "read", "不存在.md", "--json"]) == 1
        assert "404" in capsys.readouterr().err


# --------------------------------------------------------------------- write
class TestWrite:
    def test_create_persists_and_searchable(self, cli_vault, capsys):
        rel = "notes/cli新建.md"
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "write", rel, "--content", "量子纠缠排课算法正文", "--json"]
        ) == 0
        data = _json_out(capsys)
        assert data == {**data, "path": rel, "action": "created"}
        # 1) 落盘：磁盘上存在且含正文与自动 frontmatter
        raw = (cli_vault / rel).read_text(encoding="utf-8")
        assert "量子纠缠排课算法正文" in raw
        assert "created:" in raw and "updated:" in raw
        # 2) 索引可查：重新初始化（CLI 每次全量扫描）后全文检索命中
        assert kwey_cli.main(["--vault", str(cli_vault), "search", "量子纠缠", "--json"]) == 0
        hits = _json_out(capsys)["hits"]
        assert rel in [h["path"] for h in hits]

    def test_update_keeps_frontmatter(self, cli_vault, capsys):
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "write", "欢迎.md", "--content", "全新正文", "--json"]
        ) == 0
        data = _json_out(capsys)
        assert data["action"] == "updated"
        doc = (cli_vault / "欢迎.md").read_text(encoding="utf-8")
        assert "全新正文" in doc
        assert "自定义: 保留我" in doc  # 原有自定义字段保留
        assert "created:" in doc  # created 不被重置

    def test_missing_md_suffix_auto_append(self, cli_vault, capsys):
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "write", "自动补扩展名", "--content", "x", "--json"]
        ) == 0
        assert _json_out(capsys)["path"] == "自动补扩展名.md"
        assert (cli_vault / "自动补扩展名.md").is_file()

    def test_stdin_content(self, cli_vault, capsys, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO("管道写入的多行正文"))
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "write", "notes/管道.md", "--stdin", "--json"]
        ) == 0
        assert "管道写入的多行正文" in (cli_vault / "notes" / "管道.md").read_text(encoding="utf-8")

    def test_content_and_stdin_conflict_exit_2(self, cli_vault):
        with pytest.raises(SystemExit) as exc:
            kwey_cli.main(
                ["--vault", str(cli_vault), "write", "x.md", "--content", "a", "--stdin"]
            )
        assert exc.value.code == 2

    def test_content_required_exit_2(self, cli_vault):
        with pytest.raises(SystemExit) as exc:
            kwey_cli.main(["--vault", str(cli_vault), "write", "x.md"])
        assert exc.value.code == 2


# --------------------------------------------------------------------- tree
class TestTree:
    def test_json_nested_and_ignores_system_dirs(self, cli_vault, capsys):
        assert kwey_cli.main(["--vault", str(cli_vault), "tree", "--json"]) == 0
        data = _json_out(capsys)
        assert data["name"] == cli_vault.name
        tree = data["tree"]
        # 目录在前；notes/ 顶层目录存在且含子目录
        assert tree[0]["type"] == "dir"
        names = {e["name"] for e in tree}
        assert {"notes", "attachments", "欢迎.md"} <= names
        notes = next(e for e in tree if e["name"] == "notes")
        sub = {c["name"] for c in notes["children"]}
        assert {"读书", "工作"} <= sub
        # 系统/忽略目录不出现
        all_paths = json.dumps(data, ensure_ascii=False)
        assert ".kwey" not in all_paths and "templates" not in all_paths

    def test_human_ascii_tree(self, cli_vault, capsys):
        assert kwey_cli.main(["--vault", str(cli_vault), "tree"]) == 0
        out = capsys.readouterr().out
        assert f"{cli_vault.name}/" in out
        assert "├──" in out and "└──" in out
        assert "欢迎.md" in out


# --------------------------------------------------------------------- export
class TestExport:
    def test_md_byte_parity_with_disk(self, cli_vault, capsys):
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "export", "欢迎.md", "--format", "md", "--json"]
        ) == 0
        data = _json_out(capsys)
        assert data["filename"] == "欢迎.md"
        raw = (cli_vault / "欢迎.md").read_text(encoding="utf-8-sig")
        assert data["content"] == raw  # 与磁盘原文（含 frontmatter）字节级一致

    def test_html_standalone_document(self, cli_vault, capsys):
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "export", "欢迎.md", "--format", "html", "--json"]
        ) == 0
        data = _json_out(capsys)
        assert data["filename"] == "欢迎.html"
        assert data["content"].startswith("<!DOCTYPE html>")
        assert "<h1>欢迎</h1>" in data["content"]

    def test_print_html_has_page_css(self, cli_vault, capsys):
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "export", "欢迎.md", "--format", "print", "--json"]
        ) == 0
        assert "@page" in _json_out(capsys)["content"]

    def test_out_writes_file_outside_vault(self, cli_vault, capsys, tmp_path):
        out_file = tmp_path / "exports" / "plan.html"
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "export", "notes/读书/2026计划.md",
             "--format", "html", "--out", str(out_file), "--json"]
        ) == 0
        data = _json_out(capsys)
        assert data["out"] == str(out_file) and data["bytes"] > 0
        assert out_file.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_out_inside_vault_rejected_exit_2(self, cli_vault, capsys):
        inside = cli_vault / "inside.html"
        assert kwey_cli.main(
            ["--vault", str(cli_vault), "export", "欢迎.md", "--format", "md", "--out", str(inside)]
        ) == 2
        assert "vault" in capsys.readouterr().err


# --------------------------------------------------------------------- exit codes & entry
class TestUsage:
    def test_vault_required_exit_2(self):
        with pytest.raises(SystemExit) as exc:
            kwey_cli.main(["search", "x"])
        assert exc.value.code == 2

    def test_vault_dir_missing_exit_2(self, tmp_path, capsys):
        assert kwey_cli.main(["--vault", str(tmp_path / "不存在"), "tree"]) == 2
        assert "不存在" in capsys.readouterr().err

    def test_unknown_command_exit_2(self):
        with pytest.raises(SystemExit) as exc:
            kwey_cli.main(["--vault", "x", "serve"])  # 明确不实现 serve
        assert exc.value.code == 2

    def test_subprocess_smoke_utf8(self, cli_vault):
        """真实子进程冒烟：脚本入口 + --json + Windows UTF-8 stdio。"""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "kwey_cli.py"), "tree",
             "--vault", str(cli_vault), "--json"],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(PROJECT_ROOT), env=env, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        envelope = json.loads(proc.stdout)
        assert envelope["code"] == 0 and envelope["data"]["name"] == cli_vault.name
