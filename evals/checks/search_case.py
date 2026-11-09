import tools


def test_search_can_ignore_case_with_unicode(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_workspace_root", tmp_path)
    (tmp_path / "notes.txt").write_text("Straße\nother\n", encoding="utf-8")
    assert "notes.txt:1" not in tools.search_workspace("STRASSE")
    result = tools.search_workspace("STRASSE", case_sensitive=False)
    assert "notes.txt:1: Straße" in result
    schema = next(tool["function"] for tool in tools.TOOLS
                  if tool["function"]["name"] == "search_workspace")
    assert schema["parameters"]["properties"]["case_sensitive"]["type"] == "boolean"
