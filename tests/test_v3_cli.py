import builtins

import pytest

import main


@pytest.mark.parametrize(
    "args",
    [
        ["best10", "--count", "0"],
        ["best10", "--count", "-1"],
        ["best10", "--count", "NaN"],
        ["best10", "--mode", "bad"],
    ],
)
def test_bad_selection_is_rejected_before_config(args, monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda p=None: pytest.fail("invalid request loaded config"))
    with pytest.raises(SystemExit) as exc:
        main.main(args)
    assert exc.value.code == 2


def test_web_count_cli_passes_selection_to_pipeline(monkeypatch):
    cfg = object()
    monkeypatch.setattr(main, "load_config", lambda p=None: cfg)
    monkeypatch.setattr(main, "setup_logger", lambda **kw: None)
    # Real config attributes are not needed for dispatch; logger config is supplied.
    from types import SimpleNamespace

    cfg = SimpleNamespace(logging=SimpleNamespace(log_dir=".", level="INFO"))
    calls = []
    monkeypatch.setattr(main, "cmd_best10", lambda c, **kw: calls.append(kw))
    assert main.main(["best10", "--mode", "web", "--count", "25"]) == 0
    assert calls == [{"mode": "web", "limit": 25}]


def test_count_menu_retries_invalid_count_and_back_does_not_scan(monkeypatch, capsys):
    inputs = iter(["bad", "2", "-1", "nan", "13"])
    monkeypatch.setattr(builtins, "input", lambda p: next(inputs))
    assert main.choose_count("web") == 13
    assert "positive" in capsys.readouterr().out
    monkeypatch.setattr(builtins, "input", lambda p: "0")
    assert main.choose_count("mtproto") == "back"
