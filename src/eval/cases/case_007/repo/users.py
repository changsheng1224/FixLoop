"""用户模型（含故意的 None 属性访问 bug）。"""

from dataclasses import dataclass


@dataclass
class Profile:
    display_name: str


@dataclass
class User:
    name: str
    profile: Profile | None = None


def display_name(user: User) -> str:
    return user.profile.display_name  # BUG: profile 可能为 None
