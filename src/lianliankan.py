#!/usr/bin/env python3
"""Terminal tile matching with zero-based API coordinates."""

from collections import deque
import random

Board = list[list[str | None]]
Coordinate = tuple[int, int]


def new_board(rows: int = 4, cols: int = 4, seed: int | None = None) -> Board:
    """Build pairs, then shuffle using an independent random generator."""
    if any(type(value) is not int or value <= 0 for value in (rows, cols)):
        raise ValueError('rows and cols must be positive integers')
    if rows * cols % 2:
        raise ValueError('the number of cells must be even')
    if seed is not None and type(seed) is not int:
        raise ValueError('seed must be an integer or None')
    tiles = []
    for index in range(rows * cols // 2):
        symbol = 'ABCDEFGH'[index % 8]
        tiles.extend((symbol, symbol))
    random.Random(seed).shuffle(tiles)
    return [tiles[offset:offset + cols] for offset in range(0, len(tiles), cols)]


def _valid_coordinate(value: object, rows: int, cols: int) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and all(type(component) is int for component in value)
        and 0 <= value[0] < rows
        and 0 <= value[1] < cols
    )


def can_connect(board: Board, start: Coordinate, end: Coordinate) -> bool:
    """Search empty cells and one exterior ring with at most two turns."""
    rows, cols = len(board), len(board[0])
    if not all(_valid_coordinate(point, rows, cols) for point in (start, end)):
        return False
    if start == end or board[start[0]][start[1]] is None:
        return False
    if board[start[0]][start[1]] != board[end[0]][end[1]]:
        return False

    directions = ((-1, 0), (1, 0), (0, -1), (0, 1))
    pending = deque([(start[0], start[1], -1, 0)])
    visited = set()
    while pending:
        row, col, direction, turns = pending.popleft()
        for next_direction, (dr, dc) in enumerate(directions):
            next_turns = turns + int(direction != -1 and direction != next_direction)
            if next_turns > 2:
                continue
            nr, nc = row + dr, col + dc
            if not (-1 <= nr <= rows and -1 <= nc <= cols):
                continue
            if (nr, nc) == end:
                return True
            if 0 <= nr < rows and 0 <= nc < cols and board[nr][nc] is not None:
                continue
            state = (nr, nc, next_direction, next_turns)
            if state not in visited:
                visited.add(state)
                pending.append(state)
    return False


def remove_pair(board: Board, start: Coordinate, end: Coordinate) -> bool:
    """Clear exactly the selected endpoints on success."""
    if not can_connect(board, start, end):
        return False
    board[start[0]][start[1]] = None
    board[end[0]][end[1]] = None
    return True


def _display(board: Board) -> None:
    print('   ' + ' '.join(str(col + 1) for col in range(len(board[0]))))
    for row, cells in enumerate(board, 1):
        print(f'{row:2} ' + ' '.join('.' if cell is None else cell for cell in cells))


def main() -> None:
    board = new_board()
    _display(board)
    while True:
        if all(cell is None for row in board for cell in row):
            print('胜利！所有图块已消除。')
            return
        try:
            line = input('输入 r1 c1 r2 c2，或 q 退出：').strip()
        except EOFError:
            return
        if line.lower() == 'q':
            return
        try:
            r1, c1, r2, c2 = (int(part) - 1 for part in line.split())
        except ValueError:
            print('输入格式错误，请输入四个整数。')
            continue
        if remove_pair(board, (r1, c1), (r2, c2)):
            _display(board)
        else:
            print('无法消除：请选择可连接的同符号图块。')


if __name__ == '__main__':
    main()
