"""Pin the tokenizer group mapping and verify tokenization equivalence."""
import unittest

from minivectordb.sql import TOKEN, TOKEN_GROUPS, Parser, parse


class TokenizerMappingTests(unittest.TestCase):
    def test_group_index_mapping_is_pinned(self):
        # TOKEN_GROUPS[i] must be the name of regex group number i.
        self.assertEqual(TOKEN_GROUPS[1:], list(TOKEN.groupindex))
        self.assertEqual(set(TOKEN_GROUPS[1:]),
                         {'space', 'comment', 'string', 'quoted', 'number', 'name', 'symbol'})

    def test_every_token_class_round_trips(self):
        sql = ("SELECT id, `col one`, 'it''s', -12.5e3, name1 FROM t  -- comment\n"
               "WHERE a <= b AND c != d /* block */ ;")
        parser = Parser(sql)
        kinds = [kind for kind, _ in parser.tokens]
        self.assertEqual(kinds, ['name', 'name', 'symbol', 'quoted', 'symbol', 'string',
                                  'symbol', 'number', 'symbol', 'name', 'name', 'name',
                                  'name', 'name', 'symbol', 'name', 'name', 'name',
                                  'symbol', 'name', 'symbol'])

    def test_string_and_quoted_values_preserved(self):
        parser = Parser("SELECT 'a''b' , `x``y`")
        values = [value for _, value in parser.tokens]
        self.assertIn("'a''b'", values)
        self.assertIn('`x``y`', values)

    def test_invalid_input_raises_syntax_error(self):
        from minivectordb.errors import DBError
        with self.assertRaises(DBError):
            Parser('SELECT ^')

    def test_distance_select_fast_path_matches_parser(self):
        sql = ("SELECT id,L2_DISTANCE(v,'[0.1,0.2]') AS d FROM t "
               "WHERE tag='g0' ORDER BY d LIMIT 10")
        self.assertEqual(parse(sql), Parser(sql).parse())

        unfiltered = "SELECT id,L2_DISTANCE(v,'[0.1,0.2]') AS d FROM t ORDER BY d LIMIT 3;"
        self.assertEqual(parse(unfiltered), Parser(unfiltered).parse())

    def test_distance_select_fast_path_falls_back_for_other_shapes(self):
        sql = ("SELECT id,tag,L2_DISTANCE(v,'[0.1,0.2]') AS d FROM t "
               "ORDER BY d LIMIT 10")
        self.assertEqual(parse(sql), Parser(sql).parse())

    def test_insert_fast_path_matches_parser(self):
        sql = ("INSERT INTO t VALUES "
               "(1,'database','[1,0,0,0]'), "
               "(-2,'travel','[0,1,0,0]');")
        self.assertEqual(parse(sql), Parser(sql).parse())

    def test_insert_fast_path_falls_back_for_other_shapes(self):
        cases = [
            "INSERT INTO t(id,tag,v) VALUES (1,'a','[1]')",
            "INSERT INTO t VALUES (1,'a','[1]','extra')",
            "INSERT INTO t VALUES (1,'it''s','[1]')",
            "INSERT INTO `t` VALUES (1,'a','[1]')",
        ]
        for sql in cases:
            self.assertEqual(parse(sql), Parser(sql).parse())


if __name__ == '__main__':
    unittest.main()
