#!/usr/bin/env python
"""推送前密钥扫描（pre-push 钩子调用）。

设计原则：宁可漏报假警，也不要误报太吵 —— 钩子一旦老误报，人就会加 --no-verify，
防线等于不存在。所以每条规则只针对「真会花钱 / 真能登进来」的东西：

1. 历史里新增过 .env、*.pem、*.key、Keycloak 备份 —— 真正不该进仓库的配置文件
2. sk- 开头的真实密钥（排除测试假 key 与示例里的占位）
3. `xxx_CLIENTS=a=app:secret` 这种复合配置里的 secret
4. 赋值给 KEY / SECRET / TOKEN / PASSWORD 的、长度 >= 24 且不是占位符的值

用法（由 .git/hooks/pre-push 调用）：
    python scripts/pre_push_secret_scan.py
退出码 1 = 命中，拒绝推送；0 = 通过。
"""

from __future__ import annotations

import re
import subprocess
import sys

# ---------- 规则 ----------

# 不该进仓库的配置文件
DANGEROUS_FILES = re.compile(
    r"(^|/)(\.env|\.env\.(local|production|development))$"
    r"|\.(pem|key|p12|pfx|jks)$"
    r"|keycloak/.*\.json\.bak$"
)

# sk- 密钥：真实 key 远超 40 字符
SK_KEY = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{39,}")
SK_ALLOW = re.compile(r"(?i)(not-a-real|fake|dummy|example|placeholder|your[-_]?key|x{6,})")

# 赋值形态：API_KEY = "xxx" / client_secret: xxx
ASSIGN = re.compile(
    r"""(?ix)
    \b [a-z_]* (?:apikey|api_key|secret|token|password|passwd) \b
    \s* [:=] \s*
    (?: ["'] ([^"']{24,}) ["'] | ([A-Za-z0-9_-]{24,}) )
    """
)

# 复合配置：OIDC_CLIENTS=life-web=life-web:<secret>;admin-web=admin-web:<secret>
COMPOSITE = re.compile(r"\b[A-Za-z_]*CLIENTS\s*=\s*\S*?[:=]([A-Za-z0-9_-]{24,})")

VALUE_ALLOW = re.compile(
    r"(?i)(your|change|example|placeholder|here|xxxx|todo|test|dev-only|dummy|fake|none|empty|\$\{|<|\{)"
)

TEXT_EXT = re.compile(
    r"\.(py|js|mjs|ts|vue|json|ya?ml|toml|ini|cfg|conf|md|txt|sh|ps1|bat|sql|env|example|java|xml|html|css)$"
    r"|(^|/)(\.env\.[a-z]+|Dockerfile|Makefile|\.gitignore)$",
    re.IGNORECASE,
)

MAX_BYTES = 512 * 1024


def classify(line: str) -> str | None:
    """一行文本是否可疑；返回命中原因，None 表示放行。"""
    if SK_KEY.search(line) and not SK_ALLOW.search(line):
        return "sk- 密钥形态"

    m = ASSIGN.search(line)
    if m:
        value = next((v for v in m.groups() if v), "")
        if len(value) >= 24 and not VALUE_ALLOW.search(value):
            return f"疑似密钥赋值（长度 {len(value)}）"

    m = COMPOSITE.search(line)
    if m:
        value = m.group(1)
        if len(value) >= 24 and not VALUE_ALLOW.search(value):
            return f"复合配置里的 secret（长度 {len(value)}）"

    return None


def git(*args: str) -> str:
    p = subprocess.run(["git", *args], capture_output=True)
    return p.stdout.decode("utf-8", "replace")


def main() -> int:
    added = {
        line.strip()
        for line in git("log", "--all", "--diff-filter=A", "--name-only", "--pretty=format:").splitlines()
        if line.strip()
    }
    bad = sorted(f for f in added if DANGEROUS_FILES.search(f))
    if bad:
        print("推送被拒绝：历史里出现过不该提交的配置文件。", file=sys.stderr)
        for f in bad:
            print("  " + f, file=sys.stderr)
        return 1

    hits: list[str] = []
    for f in git("ls-files").splitlines():
        if not f or not TEXT_EXT.search(f):
            continue
        try:
            raw = open(f, "rb").read()
        except OSError:
            continue
        if len(raw) > MAX_BYTES:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            why = classify(line)
            if why:
                hits.append(f"{f}:{no}: {why}")

    if hits:
        print("推送被拒绝：检测到疑似密钥。", file=sys.stderr)
        for h in hits[:10]:
            print("  " + h, file=sys.stderr)
        if len(hits) > 10:
            print(f"  ……共 {len(hits)} 处", file=sys.stderr)
        return 1

    print("[pre-push] 密钥扫描通过：没有发现真实密钥。")
    return 0


if __name__ == "__main__":
    sys.exit(main())