import re
from pathlib import Path


def test_gui_assets_are_valid_utf8_and_declare_charset():
    index = Path("gui/index.html").read_text(encoding="utf-8")
    Path("gui/app.js").read_text(encoding="utf-8")
    Path("gui/styles.css").read_text(encoding="utf-8")

    assert '<meta charset="UTF-8"' in index


def test_gui_onclick_handlers_are_defined():
    index = Path("gui/index.html").read_text(encoding="utf-8")
    app = Path("gui/app.js").read_text(encoding="utf-8")

    handlers = {
        match.group(1)
        for match in re.finditer(r'onclick="([A-Za-z_$][\w$]*)\(', index)
    }
    defined = {
        match.group(1)
        for match in re.finditer(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", app)
    }

    assert handlers - defined == set()


def test_gui_exposes_api_base_and_bearer_token_settings():
    index = Path("gui/index.html").read_text(encoding="utf-8")
    app = Path("gui/app.js").read_text(encoding="utf-8")

    assert 'id="apiBaseInput"' in index
    assert 'id="apiTokenInput"' in index
    assert "tb_api_base" in app
    assert "tb_api_bearer_token" in app
    assert "sessionStorage.setItem('tb_api_bearer_token'" in app
    assert "Authorization" in app

