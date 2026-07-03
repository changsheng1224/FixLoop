"""用户标签格式化（含故意的返回值类型 bug）。"""


def user_label(user_id):
    """返回可用于拼接的用户标签片段。"""
    return user_id  # BUG: 应返回 str，否则与字符串拼接时 TypeError


def greet(user_id):
    return "User:" + user_label(user_id)
