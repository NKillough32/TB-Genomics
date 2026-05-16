from pathlib import Path


def test_gui_assets_are_valid_utf8_and_declare_charset():
    index = Path("gui/index.html").read_text(encoding="utf-8")
    Path("gui/app.js").read_text(encoding="utf-8")
    Path("gui/styles.css").read_text(encoding="utf-8")

    assert '<meta charset="UTF-8"' in index
