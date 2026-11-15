"""密钥扫描器自测：把各种样本喂给判定函数，确认该报的报、该放的不报。

注意：这里所有「看起来像密钥」的样本都是运行时用重复字符拼出来的假值，
绝不允许出现真实密钥 —— 否则这个测试文件本身就成了泄密源。
"""

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location("scan", HERE / "pre_push_secret_scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

# 假 key：结构（sk- 前缀、总分格、足够长）与真实一致，字符全是重复占位。
# 注意不能含 fake/dummy/test 等字面量，否则会被扫描器的「测试值白名单」放过，
# 导致自测测不到 sk- 规则。
FAKE_SK = "sk-" + "Zm9" * 4 + "." + "Kq7" * 4 + "." + "9" * 24
# 假 secret：32 位随机串的形态，同样用重复字符
FAKE_SECRET = "Aa1" * 8 + "Bb2" * 4

# (样本行, 是否应当被判定为「敏感」)
CASES = [
    # ---- 必须抓 ----
    (f'LLM_API_KEY={FAKE_SK}', True),
    (f'LLM_API_KEY = "{FAKE_SK}"', True),
    (f'client_secret = "{FAKE_SECRET}"', True),
    (f'OIDC_CLIENTS=life-web=life-web:{FAKE_SECRET};admin-web=admin-web:{FAKE_SECRET}', True),
    (f'api_key: sk-proj-{"Q" * 40}', True),
    # ---- 必须放过 ----
    ('KEY = "sk-test-not-a-real-key-0000"', False),
    ('LLM_API_KEY=', False),
    ('JWT_SECRET=请替换为随机32位以上字符串', False),
    ('DATABASE_URL=mysql+pymysql://root:你的密码@127.0.0.1:3306/lifeai', False),
    ('OIDC_CLIENT_SECRET=${OIDC_CLIENT_SECRET}', False),
    ('  <el-select v-model="form.category_id" class="task-form__control">', False),
    ('settings.llm_api_key.strip()', False),
    ('token = get_token(request)', False),
    ('password: str = Field(min_length=8, max_length=72)', False),
]


def main() -> int:
    bad = 0
    for line, want in CASES:
        got = scan.classify(line) is not None
        mark = "ok " if got == want else "FAIL"
        if got != want:
            bad += 1
        shown = line if len(line) <= 60 else line[:57] + "..."
        print(f"[{mark}] 期望={('报' if want else '放')} 实际={('报' if got else '放')}  {shown}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())