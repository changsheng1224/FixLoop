"""SWE-bench Lite 开发集：首次选定后不因分数更换。"""

from __future__ import annotations

# 固定 5 个 Lite 开发实例（Adapter 冒烟 / 端到端接入，不作最终成绩）
DEV_INSTANCE_IDS: tuple[str, ...] = (
    "astropy__astropy-12907",
    "django__django-11099",
    "matplotlib__matplotlib-23964",
    "pylint-dev__pylint-6506",
    "sympy__sympy-20590",
)

DATASET_NAME = "princeton-nlp/SWE-bench_Lite"
DATASET_SPLIT = "test"
