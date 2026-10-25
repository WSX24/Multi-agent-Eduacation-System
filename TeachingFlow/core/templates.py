# -*- coding: utf-8 -*-
"""模板渲染入口：一律先过输入契约预检，再渲染。

为什么不直接 ``PromptLib.build``：模板的 ``inputs:`` 块声明了类型与单位
（如 总周期=quantity/周、学生水平=enum）。跳过预检会让「每周三小时」这类
自由文本静默降级成模型猜数字，且不报错。``require_inputs`` 是仓库里
既有的强制版预检，这里只在外面加一层缓存与提示。
"""
from __future__ import annotations

from .config import PACK_DIR, PROMPT_LIB_DIR

from prompt_builder import PromptLib            # noqa: E402  (config 已注入 sys.path)
from quality_gate import require_inputs          # noqa: E402

#: 主模板库（PromptTemplate/，仓库既有资产，只读）
MAIN = PromptLib(PROMPT_LIB_DIR)
#: 本链路自有模板（course_map 等），与主库同名时以主库为准
PACK = PromptLib(PACK_DIR)


def _lib_for(role: str) -> PromptLib:
    if role in MAIN.templates:
        return MAIN
    if role in PACK.templates:
        return PACK
    raise KeyError(f"未知模板 {role!r}；主库={MAIN.list_roles()} 自有={PACK.list_roles()}")


def build(role: str, variables: dict, fewshot: bool | str = True,
          cot: str | None = None) -> str:
    """预检 + 渲染。返回的就是「实际发给 LLM 的提示词」。"""
    lib = _lib_for(role)
    require_inputs(lib, role, variables)
    return lib.build(role, variables, fewshot=fewshot, cot=cot)


def check(role: str, variables: dict) -> list[str]:
    """非抛异常版预检：返回问题描述列表（供 --mock 演示与排错）。"""
    lib = _lib_for(role)
    return [f"{p.name}：{p.detail}" for p in lib.check_inputs(role, variables)]
