from dataclasses import dataclass


@dataclass
class Profile:
    name: str
    email: str


def greet(p: Profile) -> str:
    return f"Hello {p.name}"
