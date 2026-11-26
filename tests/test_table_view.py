"""CSV/TSV 表格模型：分隔符探测、无损往返、编码/BOM 与接口。"""

from __future__ import annotations

from app.viewers.table import build_table, serialize_table


class TestBuildTable:
    def test_comma_lf(self):
        model = build_table("名称,数量\n苹果,3\n", ".csv")
        assert model is not None
        assert model["delimiter"] == ","
        assert model["rows"] == [["名称", "数量"], ["苹果", "3"]]
        assert model["trailing_newline"] is True
        assert model["row_count"] == 2 and model["col_count"] == 2

    def test_tsv_prefers_tab(self):
        model = build_table("a\tb\n1\t2\n", ".tsv")
        assert model is not None
        assert model["delimiter"] == "\t"
        assert model["rows"] == [["a", "b"], ["1", "2"]]

    def test_semicolon_and_pipe(self):
        assert build_table("a;b\n1;2\n", ".csv")["delimiter"] == ";"
        assert build_table("a|b\n1|2\n", ".csv")["delimiter"] == "|"

    def test_quoted_delimiter_and_newline(self):
        content = '"a,b",c\n"x\ny",z\n'
        model = build_table(content, ".csv")
        assert model is not None
        assert model["rows"][0] == ["a,b", "c"]
        assert model["rows"][1] == ["x\ny", "z"]
        assert serialize_table(model["rows"], ",", "\n", True) == content

    def test_missing_trailing_newline(self):
        content = "a,b\n1,2"
        model = build_table(content, ".csv")
        assert model is not None
        assert model["trailing_newline"] is False
        assert serialize_table(model["rows"], ",", "\n", False) == content

    def test_crlf_roundtrip(self):
        content = "a,b\r\n1,2\r\n"
        model = build_table(content, ".csv")
        assert model is not None
        assert model["trailing_newline"] is True
        assert serialize_table(model["rows"], ",", "\r\n", True) == content

    def test_single_column_falls_back(self):
        # 单列无法可靠判断分隔符，按不安全处理
        assert build_table("单列\n第二行\n", ".csv") is None

    def test_single_empty_field_row_falls_back(self):
        assert build_table('""\n', ".csv") is None

    def test_ragged_rows_fall_back(self):
        assert build_table("a,b\n1,2,3\n", ".csv") is None

    def test_broken_quotes_fall_back(self):
        assert build_table('a,b\n"未闭合,引号\n', ".csv") is None

    def test_redundant_quotes_fall_back(self):
        # 原文带冗余引号，无损重写会去掉 → 视为不安全，退回源码
        assert build_table('"a",b\n1,2\n', ".csv") is None

    def test_empty_content(self):
        assert build_table("", ".csv") is None

    def test_row_limit(self, monkeypatch):
        import app.viewers.table as table

        monkeypatch.setattr(table, "MAX_TABLE_ROWS", 2)
        assert build_table("a,b\n1,2\n3,4\n", ".csv") is None


class TestPlainTextTableService:
    def test_read_csv_includes_table(self, vault):
        root = vault.paths.ensure_root()
        (root / "表.csv").write_bytes("名称,数量\r\n苹果,3\r\n".encode("utf-8"))

        data = vault.read_plain_text("表.csv")
        assert data["lossy"] is False
        assert data["table"]["delimiter"] == ","
        assert data["table"]["rows"][1] == ["苹果", "3"]
        assert data["newline"] == "\r\n"
        assert data["table_reason"] == ""

    def test_read_tsv_delimiter(self, vault):
        root = vault.paths.ensure_root()
        (root / "表.tsv").write_bytes(b"a\tb\n1\t2\n")
        data = vault.read_plain_text("表.tsv")
        assert data["table"]["delimiter"] == "\t"

    def test_gbk_bytes_preserved_roundtrip(self, vault):
        root = vault.paths.ensure_root()
        original = "名称,数量\r\n苹果,3\r\n".encode("gbk")
        (root / "gbk.csv").write_bytes(original)

        data = vault.read_plain_text("gbk.csv")
        assert data["encoding"] == "gb18030"
        assert data["table"] is not None
        rebuilt = serialize_table(
            data["table"]["rows"],
            data["table"]["delimiter"],
            data["newline"],
            data["table"]["trailing_newline"],
        )
        vault.write_plain_text("gbk.csv", rebuilt, base_mtime=data["mtime"])
        assert (root / "gbk.csv").read_bytes() == original

    def test_unsafe_csv_has_no_table(self, vault):
        root = vault.paths.ensure_root()
        (root / "bad.csv").write_bytes('"a",b\n1,2\n'.encode("utf-8"))
        data = vault.read_plain_text("bad.csv")
        assert data["table"] is None
        assert "源码" in data["table_reason"]

    def test_txt_has_no_table_key(self, vault):
        root = vault.paths.ensure_root()
        (root / "a.txt").write_bytes("纯文本\n".encode("utf-8"))
        assert "table" not in vault.read_plain_text("a.txt")


class TestPlainTextTableApi:
    def test_api_table_and_untouched_roundtrip(self, client, vault):
        root = vault.paths.ensure_root()
        original = "名称,数量\r\n苹果,3\r\n香蕉,5\r\n".encode("utf-8")
        (root / "接口.csv").write_bytes(original)

        data = client.get("/api/plain-file", params={"path": "接口.csv"}).json()["data"]
        assert data["table"]["rows"][0] == ["名称", "数量"]

        rebuilt = serialize_table(
            data["table"]["rows"],
            data["table"]["delimiter"],
            data["newline"],
            data["table"]["trailing_newline"],
        )
        assert rebuilt == data["content"]

        put = client.put(
            "/api/plain-file",
            json={"path": "接口.csv", "content": rebuilt, "base_mtime": data["mtime"]},
        )
        assert put.status_code == 200
        assert (root / "接口.csv").read_bytes() == original

    def test_api_table_after_cell_edit_keeps_shape(self, client, vault):
        root = vault.paths.ensure_root()
        (root / "编辑.csv").write_bytes("a,b\n1,2\n".encode("utf-8"))
        data = client.get("/api/plain-file", params={"path": "编辑.csv"}).json()["data"]

        rows = [list(row) for row in data["table"]["rows"]]
        rows[1][1] = "改了"
        rebuilt = serialize_table(rows, data["table"]["delimiter"], data["newline"], True)
        assert rebuilt == "a,b\n1,改了\n"

        client.put(
            "/api/plain-file",
            json={"path": "编辑.csv", "content": rebuilt, "base_mtime": data["mtime"]},
        )
        assert (root / "编辑.csv").read_bytes() == rebuilt.encode("utf-8")
