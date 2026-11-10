"""config 与 .env.example 的一致性测试。

为什么要测这个文件：
    .env.example 是别人 clone 仓库后第一个照着填的东西。它一旦和 Settings
    漂移（多出代码不读的键、漏掉必填的键），下一个人会填了一堆不生效的配置，
    然后花半天怀疑代码。这类问题跑一次测试就能挡住，不值得靠人肉 review。

漂移的典型翻车方式：V1.1 加 AI 配置时新写了 LLM_* 一整套，旧的 AI_* /
DEEPSEEK_API_KEY 没删 —— 模板里同一个东西有两份，其中一份是死的。
"""

import re
from pathlib import Path

from core.config import BACKEND_DIR, Settings

EXAMPLE = BACKEND_DIR / ".env.example"

# .env.example 里的键 → Settings 字段名（大小写不敏感，这里统一小写比较）
_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _example_keys() -> list[str]:
    text = EXAMPLE.read_text(encoding="utf-8")
    return [
        m.group(1).lower()
        for m in (_LINE_RE.match(line) for line in text.splitlines())
        if m
    ]


def test_env_example_exists():
    assert EXAMPLE.is_file(), ".env.example 丢了，别人没法知道要配什么"


def test_every_example_key_is_read_by_code():
    """模板里不许有代码不读的键 —— 填了不生效比没写更误导。"""
    fields = {k.lower() for k in Settings.model_fields}
    unknown = [k for k in _example_keys() if k not in fields]
    assert unknown == [], f".env.example 里有 Settings 读不到的键：{unknown}"


def test_every_setting_is_documented():
    """每个字段都该在模板里出现，否则就是「只有作者知道要配它」。"""
    documented = set(_example_keys())
    missing = sorted(k for k in Settings.model_fields if k.lower() not in documented)
    assert missing == [], f"这些 Settings 字段在 .env.example 里没有说明：{missing}"


def test_example_has_no_duplicate_keys():
    keys = _example_keys()
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert dupes == [], f".env.example 里重复定义：{dupes}"


def test_required_values_are_left_blank():
    """命门项留空：模板里写假值会让人以为已经配好了。

    DATABASE_URL 例外 —— 它必须带结构（驱动 + 库名），只把密码位置留成中文占位。
    """
    text = EXAMPLE.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        if m := _LINE_RE.match(line):
            values[m.group(1).lower()] = line.split("=", 1)[1].strip()

    # 密钥类一律留空：留空时行为是「明确失败」，填假值才是「安静地不安全工作」
    assert values["jwt_secret"] == ""
    assert values["llm_api_key"] == ""

    # 数据库串要有结构，但密码必须是占位符而不是真密码
    db = values["database_url"]
    assert "mysql+pymysql://" in db
    assert ":3306/lifeai?" in db
    password_part = db.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert password_part and not password_part.isascii(), "模板里不该有能直接用的密码"


def test_example_contains_no_secret_placeholder_lookalike():
    """模板里不许出现像真 key 的东西，免得被 gitleaks 之类的扫描器误报。"""
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "sk-" not in text.replace("sk-请替换", ""), "模板里残留了 sk- 开头的占位符"