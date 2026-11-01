"""Standard-library tests; execution evidence must be collected separately."""

from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import random
import subprocess
import sys
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
import lianliankan as game


class GenerationTests(unittest.TestCase):
    def test_default_counts_and_reproducibility(self):
        for seed in (None, 0, 1, 19, -7):
            with self.subTest(seed=seed):
                board = game.new_board(seed=seed)
                self.assertEqual([len(row) for row in board], [4] * 4)
                self.assertEqual(Counter(cell for row in board for cell in row),
                                 Counter({symbol: 2 for symbol in 'ABCDEFGH'}))
                if seed is not None:
                    self.assertEqual(board, game.new_board(seed=seed))

    def test_sizes_rows_and_global_random_state(self):
        state = random.getstate()
        for rows, cols in ((1, 2), (2, 3), (6, 8)):
            board = game.new_board(rows, cols, seed=41)
            self.assertEqual(len(board), rows)
            self.assertTrue(all(len(row) == cols for row in board))
            self.assertEqual(len({id(row) for row in board}), rows)
            counts = Counter(cell for row in board for cell in row)
            self.assertTrue(all(count % 2 == 0 for count in counts.values()))
            self.assertEqual(board, game.new_board(rows, cols, seed=41))
        self.assertEqual(random.getstate(), state)

    def test_pairs_exist_before_shuffle(self):
        class RecordingRandom:
            def __init__(self, seed):
                self.seed = seed

            def shuffle(self, tiles):
                self_before.append(list(tiles))
                tiles.reverse()

        self_before = []
        with patch.object(game.random, 'Random', RecordingRandom):
            board = game.new_board(2, 4, seed=5)
        self.assertEqual(self_before, [list('AABBCCDD')])
        self.assertEqual(board, [list('DDCC'), list('BBAA')])

    def test_invalid_dimensions_and_seed(self):
        for value in (True, False, 0, -1, 2.0, '2', None):
            for dimensions in ((value, 4), (4, value)):
                with self.subTest(dimensions=dimensions):
                    with self.assertRaises(ValueError):
                        game.new_board(*dimensions)
        with self.assertRaises(ValueError):
            game.new_board(3, 3)
        for seed in (True, False, 1.0, '1', [], {}):
            with self.subTest(seed=seed):
                with self.assertRaises(ValueError):
                    game.new_board(seed=seed)


class ConnectionTests(unittest.TestCase):
    def assert_connection(self, board, start, end, expected):
        before = deepcopy(board)
        rows = [id(row) for row in board]
        self.assertIs(game.can_connect(board, start, end), expected)
        self.assertEqual(board, before)
        self.assertEqual([id(row) for row in board], rows)
        self.assertIs(game.can_connect(board, end, start), expected)
        self.assertEqual(board, before)

    def test_straight_and_adjacent(self):
        self.assert_connection([['A', None, 'A']], (0, 0), (0, 2), True)
        self.assert_connection([['A'], [None], ['A']], (0, 0), (2, 0), True)
        self.assert_connection([['A', 'A']], (0, 0), (0, 1), True)

    def test_one_turn(self):
        board = [['A', None], ['X', 'A']]
        self.assert_connection(board, (0, 0), (1, 1), True)

    def test_two_turns_inside(self):
        board = [list('XXXXX'), ['X', 'A', None, 'X', 'X'],
                 ['X', 'X', None, 'X', 'X'],
                 ['X', 'X', None, 'A', 'X'], list('XXXXX')]
        self.assert_connection(board, (1, 1), (3, 3), True)

    def test_outer_ring_on_each_side(self):
        boards = [([list('AXA')], (0, 0), (0, 2)),
                  ([list('XXX'), list('AXA')], (1, 0), (1, 2)),
                  ([['A', 'X'], ['X', 'X'], ['A', 'X']], (0, 0), (2, 0)),
                  ([['X', 'A'], ['X', 'X'], ['X', 'A']], (0, 1), (2, 1))]
        for board, start, end in boards:
            with self.subTest(board=board):
                self.assert_connection(board, start, end, True)

    def test_three_turn_corridor_is_rejected(self):
        board = [list('XXXXX'), ['X', 'A', None, 'X', 'X'],
                 ['X', 'X', None, None, 'X'],
                 ['X', 'X', 'X', 'A', 'X'], list('XXXXX')]
        # The only corridor is right, down, right, down; walls seal the exterior.
        path = [(1, 1), (1, 2), (2, 2), (2, 3), (3, 3)]
        directions = []
        for first, second in zip(path, path[1:]):
            delta = (second[0] - first[0], second[1] - first[1])
            self.assertEqual(abs(delta[0]) + abs(delta[1]), 1)
            directions.append(delta)
        self.assertEqual(sum(a != b for a, b in zip(directions, directions[1:])), 3)
        self.assertTrue(all(board[r][c] is None for r, c in path[1:-1]))
        self.assert_connection(board, (1, 1), (3, 3), False)
        before = deepcopy(board)
        self.assertFalse(game.remove_pair(board, (1, 1), (3, 3)))
        self.assertEqual(board, before)

    def test_blocked(self):
        board = [list('XXXXX'), list('XAXAX'), list('XXXXX')]
        self.assert_connection(board, (1, 1), (1, 3), False)

    def test_invalid_selections_preserve_board_and_rows(self):
        board = [['A', 'A', 'B'], [None, 'X', None]]
        invalid = [(-1, 0), (2, 0), (0, -1), (0, 3),
                   None, 1, '00', [0, 0], (), (0,), (0, 0, 0),
                   (True, 0), (0, False), (0.0, 0), ('0', 0)]
        pairs = [(point, (0, 1)) for point in invalid]
        pairs += [((0, 0), point) for point in invalid]
        pairs += [((0, 0), (0, 0)), ((0, 0), (0, 2)),
                  ((1, 0), (0, 0)), ((0, 0), (1, 2)), ((1, 0), (1, 2))]
        for start, end in pairs:
            with self.subTest(start=start, end=end):
                before = deepcopy(board)
                rows = [id(row) for row in board]
                self.assert_connection(board, start, end, False)
                self.assertFalse(game.remove_pair(board, start, end))
                self.assertEqual(board, before)
                self.assertEqual([id(row) for row in board], rows)

    def test_remove_only_endpoints_and_repeat(self):
        board = [['A', None, 'A'], ['B', 'C', 'B']]
        rows = [id(row) for row in board]
        self.assertTrue(game.remove_pair(board, (0, 0), (0, 2)))
        expected = [[None, None, None], ['B', 'C', 'B']]
        self.assertEqual(board, expected)
        self.assertEqual([id(row) for row in board], rows)
        self.assertFalse(game.remove_pair(board, (0, 0), (0, 2)))
        self.assertEqual(board, expected)


class TerminalTests(unittest.TestCase):
    def test_mapping_errors_display_and_victory(self):
        board = [[None, 'A'], [None, 'A']]
        output = io.StringIO()
        inputs = ['', 'x', '1 2 3', '1 2 3 4 5', '0 2 2 2', ' 1 2 2 2 ']
        with patch.object(game, 'new_board', return_value=board), \
             patch('builtins.input', side_effect=inputs) as reader, \
             patch.object(game, 'remove_pair', wraps=game.remove_pair) as remover, \
             redirect_stdout(output):
            game.main()
        self.assertEqual(reader.call_count, len(inputs))
        self.assertEqual(remover.call_args_list[-1].args[1:], ((0, 1), (1, 1)))
        self.assertEqual(board, [[None, None], [None, None]])
        text = output.getvalue()
        self.assertIn('   1 2', text)
        self.assertIn(' 1 . A', text)
        self.assertIn(' 2 . A', text)
        self.assertIn(' 1 . .', text)
        self.assertIn(' 2 . .', text)
        self.assertIn('格式错误', text)
        self.assertIn('无法消除', text)
        self.assertIn('胜利', text)

    def test_mock_quit_and_eof(self):
        for response in (' q ', ' Q ', EOFError()):
            with self.subTest(response=response):
                with patch.object(game, 'new_board', return_value=[['A', 'A']]), \
                     patch('builtins.input', side_effect=[response]) as reader, \
                     redirect_stdout(io.StringIO()):
                    game.main()
                self.assertEqual(reader.call_count, 1)

    def test_script_quit_and_eof(self):
        for supplied_input in ('q\n', 'Q\n', ''):
            with self.subTest(supplied_input=supplied_input):
                result = subprocess.run(
                    [sys.executable, 'lianliankan.py'], cwd=SRC,
                    input=supplied_input, text=True, encoding='utf-8',
                    capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                lines = result.stdout.splitlines()
                self.assertEqual(lines[0].split(), ['1', '2', '3', '4'])
                self.assertEqual([line.split()[0] for line in lines[1:5]],
                                 ['1', '2', '3', '4'])
                self.assertTrue(all(len(line.split()) == 5 for line in lines[1:5]))

    def test_shebang_and_silent_import(self):
        source = (SRC / 'lianliankan.py').read_text(encoding='utf-8')
        self.assertEqual(source.splitlines()[0], '#!/usr/bin/env python3')
        probe = (
            'import builtins; '
            'builtins.input = lambda *args: (_ for _ in ()).throw('
            'RuntimeError("input during import")); '
            'import lianliankan; '
            'assert all(callable(getattr(lianliankan, name)) for name in '
            '("main", "new_board", "can_connect", "remove_pair"))'
        )
        result = subprocess.run([sys.executable, '-c', probe], cwd=SRC,
                                input='', text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
