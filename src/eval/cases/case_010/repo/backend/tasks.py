"""任务执行（含类型运算 bug）。"""


def run_task(a, b):
    return a + b  # BUG: 未处理 str 操作数
