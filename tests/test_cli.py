import sys

import pytest

from covenant import __main__, config


@pytest.mark.parametrize("cli_url, env_url, expected", [
    ("https://fresh.example.com/", "invalid-old-value", "https://fresh.example.com"),
    (None, "https://environment.example.com", "https://environment.example.com"),
    (None, None, "https://file.example.com"),
])
def test_serve_url_precedence_without_changing_env(tmp_path, monkeypatch, cli_url, env_url, expected):
    env_file = tmp_path / ".env"
    original = "PORT=7777\nPUBLIC_BASE_URL=https://file.example.com\n"
    env_file.write_text(original)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "chat.sqlite3"))
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("BIND_HOST", raising=False)
    if env_url is None:
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("PUBLIC_BASE_URL", env_url)
    args = ["covenant", "serve"]
    if cli_url is not None:
        args += ["--public-base-url", cli_url]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(__main__.os, "umask", lambda value: None)
    launched = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: launched.append((app, kwargs)))

    __main__.main()

    app, kwargs = launched[0]
    assert app.state.settings.public_base_url == expected
    assert expected in app.state.settings.allowed_origins
    assert kwargs["port"] == 7777
    assert env_file.read_text() == original


@pytest.mark.parametrize("url", ["http://public.example.com", "https://chat.example.com/room", ""])
def test_serve_rejects_invalid_public_url(tmp_path, monkeypatch, capsys, url):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "chat.sqlite3"))
    monkeypatch.setenv("PORT", "6666")
    monkeypatch.setattr(sys, "argv", ["covenant", "serve", "--public-base-url", url])
    monkeypatch.setattr(__main__.os, "umask", lambda value: None)

    with pytest.raises(SystemExit) as exc:
        __main__.main()

    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "chat.sqlite3").exists()
