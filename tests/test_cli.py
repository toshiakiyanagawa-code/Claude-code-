from podedit.cli import _browser_url


def test_browser_url_prefers_codespaces_forwarded_url(monkeypatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "sample-space")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")

    assert _browser_url("0.0.0.0", 8765) == "https://sample-space-8765.app.github.dev"


def test_browser_url_rewrites_wildcard_host_to_loopback(monkeypatch) -> None:
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    monkeypatch.delenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", raising=False)

    assert _browser_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"


def test_browser_url_brackets_ipv6(monkeypatch) -> None:
    monkeypatch.delenv("CODESPACE_NAME", raising=False)
    monkeypatch.delenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", raising=False)

    assert _browser_url("::1", 8765) == "http://[::1]:8765"
