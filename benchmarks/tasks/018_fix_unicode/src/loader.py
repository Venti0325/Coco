from pathlib import Path


def load_data() -> str:
    # Bug: data.txt is UTF-8 but we force ascii
    return Path("data.txt").read_text(encoding="ascii")


if __name__ == "__main__":
    print(load_data())
