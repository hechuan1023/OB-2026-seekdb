"""Pin the tokenizer group mapping and verify tokenization equivalence."""
import unittest

from minivectordb.sql import TOKEN, TOKEN_GROUPS, Parser


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


if __name__ == '__main__':
    unittest.main()
