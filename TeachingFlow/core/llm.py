# -*- coding: utf-8 -*-
"""LLM 出口。真实调用与 mock 走同一个签名，链路代码不用分支。

mock 模式下，每个 step 读 ``fixtures/<step>.txt`` 当作「LLM 的回复」——
这样能在不花 token 的情况下验证 5 跳数据流是否贯通（骨架验证）。
"""
from __future__ import annotations

from pathlib import Path

from .config import FIXTURES_DIR, api_config


class LLMError(RuntimeError):
    pass


#: 步骤名 → 中文标签（归档文件名用，顺序即阅读顺序）
STEP_LABELS = {
    "s0_scope": "S0-模块切分",
    "s0_module": "S0a-模块判定",
    "s0_estimate": "S0b-周期估算",
    "s1_plan": "S1-学习方案",
    "s3_unit": "S3-单元教学",
    "s4_pages": "S4a-页数决策",
    "s4_outline": "S4-课件大纲",
}


class LLM:
    def __init__(self, mock: bool = False, fixtures_dir: Path | None = None,
                 temperature: float = 0.7, run=None):
        self.mock = mock
        self.fixtures_dir = fixtures_dir or FIXTURES_DIR
        self.temperature = temperature
        self.run = run                   # 给了就把每次调用归档到 <run>/LLM返回/
        self.calls: list[dict] = []      # 供 manifest 记录：step / 字符数 / 温度

    # ---------------------------------------------------------------- 入口
    def call(self, step: str, prompt: str, temperature: float | None = None,
             tag: str = "") -> str:
        """按 step 名调模型（或读 fixture）。

        step 只用于 mock 选文件与日志，不参与真实请求。
        """
        temp = self.temperature if temperature is None else temperature
        label = STEP_LABELS.get(step, step) + (f"·{tag}" if tag else "")
        if self.mock:
            text = self._fixture(step)
            self.calls.append({"step": step, "mock": True, "chars": len(text)})
            self._archive(label, step, prompt, text,
                          {"model": "fixture", "temperature": temp, "mock": True})
            return text

        if not prompt.strip():
            raise LLMError(f"{step}：提示词为空")

        key, base_url, model = api_config()
        if not key:
            return self._fail(step, "未找到 API Key（PPT-Agent/.env 的 DEEPSEEK_API_KEY）")

        try:
            from openai import OpenAI
        except ImportError:
            return self._fail(step, "未安装 openai：pip install openai")

        client = OpenAI(api_key=key, base_url=base_url, timeout=600)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temp,
            )
        except Exception as exc:                       # noqa: BLE001
            raise LLMError(f"{step}：API 调用失败（{type(exc).__name__}）：{exc}") from exc

        text = (resp.choices[0].message.content or "").strip()
        self.calls.append({"step": step, "model": model, "temperature": temp,
                           "chars": len(text)})
        if not text:
            raise LLMError(f"{step}：模型返回为空")
        self._archive(label, step, prompt, text,
                      {"model": model, "temperature": temp, "mock": False})
        return text

    def _archive(self, label: str, step: str, prompt: str, raw: str,
                 meta: dict) -> None:
        """把这次调用收进 ``<run>/LLM返回/``（无 run 时静默跳过）。"""
        if self.run is None:
            return
        try:
            self.run.record_llm(len(self.calls), label, step, prompt, raw, meta)
        except Exception as exc:                      # noqa: BLE001
            print(f"   ⚠️ LLM 返回归档失败（{type(exc).__name__}）：{exc}")

    # ---------------------------------------------------------------- 内部
    def _fixture(self, step: str) -> str:
        path = self.fixtures_dir / f"{step}.txt"
        if not path.is_file():
            raise LLMError(f"--mock 缺 fixture：{path}（应为该步骤的假 LLM 回复）")
        return path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _fail(step: str, msg: str) -> str:
        raise LLMError(f"{step}：{msg}")


def run_with_gate(llm: LLM, step: str, prompt: str, check, temps=(0.7, 0.4, 0.0),
                  tag: str = ""):
    """调模型 → 过 gate → 不过则降温重试。返回 ``(raw, payload, gate, attempts)``。

    ``check(raw) -> (payload, gate)``，gate 需要有 ``.ok`` / ``.failures``。
    最后一次仍不过也把结果返回（由调用方决定是终止还是降级），
    因为「模型已尽力」的产物带上缺陷清单仍比丢掉从零重跑有价值。
    """
    tries = temps[:1] if llm.mock else temps      # mock 下 fixture 不会变，重试无意义
    raw, payload, gate = "", None, None
    for i, temp in enumerate(tries, 1):
        raw = llm.call(step, prompt, temperature=temp, tag=tag)
        payload, gate = check(raw)
        if gate.ok:
            return raw, payload, gate, i
        if i < len(tries):
            print(f"   ⚠️ 第 {i} 轮未过 gate（{'；'.join(n for n, _ in gate.failures)}），"
                  f"降温至 {tries[i]} 重试 …")
    return raw, payload, gate, len(tries)
