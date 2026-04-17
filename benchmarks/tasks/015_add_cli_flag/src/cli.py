import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="greet")
    p.add_argument("--name", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"Hello {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
