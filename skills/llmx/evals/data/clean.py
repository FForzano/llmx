from pathlib import Path

def read_lines(path: Path) -> list[str]:
    with path.open() as f:
        return [line.rstrip("\n") for line in f]

def count_words(lines: list[str]) -> int:
    return sum(len(line.split()) for line in lines)
