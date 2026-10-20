"""VlmPool.pin 把所有角色收成一家，禁止降级。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client.vlm import VlmPool

YAML = """
providers:
  fake_a:
    base_url: http://127.0.0.1:9
    model: a
    api_key_env: TEST_VLM_PIN_A
  fake_b:
    base_url: http://127.0.0.1:9
    model: b
    api_key_env: TEST_VLM_PIN_B
    enabled: false
roles:
  primary: [fake_a, fake_b]
  coarse: [fake_b, fake_a]
"""


def test_pin_locks_all_roles(tmp_path: Path):
    os.environ["TEST_VLM_PIN_A"] = "dummy"
    os.environ.pop("TEST_VLM_PIN_B", None)
    y = tmp_path / "providers.yaml"
    y.write_text(YAML, encoding="utf-8")
    pool = VlmPool(providers_path=y, use_cache=False)
    assert pool.chain("primary") == ["fake_a"]
    pool.pin("fake_a")
    assert pool.chain("primary") == ["fake_a"]
    assert pool.chain("coarse") == ["fake_a"]
    names, err = pool._chain_or_named("primary")
    assert names == ["fake_a"] and not err


def test_pin_unknown_raises(tmp_path: Path):
    os.environ["TEST_VLM_PIN_A"] = "dummy"
    y = tmp_path / "providers.yaml"
    y.write_text(YAML, encoding="utf-8")
    pool = VlmPool(providers_path=y, use_cache=False)
    try:
        pool.pin("no_such")
        assert False, "应该抛 ValueError"
    except ValueError as e:
        assert "未知" in str(e)


def test_pin_disabled_raises(tmp_path: Path):
    os.environ["TEST_VLM_PIN_A"] = "dummy"
    y = tmp_path / "providers.yaml"
    y.write_text(YAML, encoding="utf-8")
    pool = VlmPool(providers_path=y, use_cache=False)
    try:
        pool.pin("fake_b")
        assert False, "应该抛 RuntimeError"
    except RuntimeError as e:
        assert "停用" in str(e)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        test_pin_locks_all_roles(p)
        test_pin_unknown_raises(p)
        test_pin_disabled_raises(p)
    print("ok")
