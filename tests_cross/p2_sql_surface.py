"""独立交叉验证 P2：SQL 表面、类型边界、转义输出、有限内存排序、协议行为。

真值独立建立；所有预期先按赛题要求与公开题包断言规则推导，再对照实现。
覆盖前三组（p0/p1）未涉及的路径：
  - 解析器表面：注释、转义引号、串内分号、多行 SQL、反引号标识符、大小写
  - 类型边界：BIGINT 2^63 边界、VARCHAR 按 Unicode 字符计长、码点比较
  - 非法向量全谱：NaN/Infinity/溢出/维度/空/非数/布尔/嵌套/JSON 语法错
  - 不支持语法必须报错：OR/IN/NOT/聚合/JOIN/NULL/主键更新/重复赋值
  - 输出转义：反斜杠、Tab、换行、回车、单引号；向量紧凑 JSON 精确文本
  - 有限内存排序：4KiB 预算外部归并、TopK 剪枝、DESC 并列、临时文件清理
  - 协议行为：注释行无响应、未终止字符串、模式控制行仅限完整语句之间
  - 超大维度：65536（原生上限内）与 65537（Python 回退）
"""
import glob
import json
import math
import os
import queue
import random
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSED = []
FAILED = []


def check(name, condition, detail=''):
    (PASSED if condition else FAILED).append(name)
    print(('PASS ' if condition else 'FAIL ') + name + ((' :: ' + str(detail)) if detail and not condition else ''))


def f32(value):
    return struct.unpack('f', struct.pack('f', value))[0]


def parse_float(text):
    got = float(text)
    assert math.isfinite(got), text
    return got


def close_enough(actual, expected, tol=1e-5):
    return abs(actual - expected) <= tol + tol * abs(expected)


class Pipe:
    def __init__(self, directory, extra=(), ready=True, env_extra=None):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        env.pop('MINIVECTORDB_FAILPOINT', None)
        if env_extra:
            env.update(env_extra)
        self.process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'minivectordb', '--data-dir', str(directory), *extra],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', bufsize=1)
        self.lines = queue.Queue()
        self.stderr_lines = []
        for target, sink in ((self.process.stdout, self.lines), (self.process.stderr, self.stderr_lines)):
            threading.Thread(target=self._pump, args=(target, sink), daemon=True).start()
        if ready:
            assert self.line(timeout=30) == 'READY'

    def _pump(self, stream, sink):
        for line in stream:
            if isinstance(sink, queue.Queue):
                sink.put(line.rstrip('\r\n'))
            else:
                sink.append(line.rstrip('\r\n'))
        if isinstance(sink, queue.Queue):
            sink.put(None)

    def line(self, timeout=30):
        value = self.lines.get(timeout=timeout)
        assert value is not None, f'unexpected exit; stderr={self.stderr_lines}'
        return value

    def raw(self, text):
        self.process.stdin.write(text)
        self.process.stdin.flush()

    def execute(self, sql, timeout=30):
        self.raw(sql + ';\n')
        first = self.line(timeout)
        if first.startswith('RESULT\t'):
            rows = []
            while True:
                row = self.line(timeout)
                if row.startswith('END\t'):
                    return first, rows
                rows.append(row.split('\t'))
        return first, None

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        self.process.wait(timeout=30)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def fresh():
    temp = tempfile.TemporaryDirectory(prefix='cross-p2-')
    return temp, Path(temp.name)


def rows_of(pipe, sql):
    head, rows = pipe.execute(sql)
    assert head.startswith('RESULT\t'), (sql, head)
    return rows


def expect_error(pipe, name, sql, code):
    head, _ = pipe.execute(sql)
    check(f'{name}', head.startswith(f'ERROR\t{code}\t'), head)


def expect_ok(pipe, name, sql, affected=None):
    head, _ = pipe.execute(sql)
    ok = head.startswith('OK\t') and (affected is None or head == f'OK\t{affected}')
    check(f'{name}', ok, head)


def main():
    # ================= S1：解析器与语句表面 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S1a1 建表（混合大小写关键字）',
                      'CrEaTe TaBlE s1 (Id BiGiNt PrImArY KeY, vAlUe BiGiNt)', 0)
            expect_ok(pipe, 'S1a2 BEGIN TRANSACTION 完整拼写', 'BEGIN TRANSACTION', 0)
            expect_ok(pipe, 'S1a3 插入（混合大小写）', 'InSeRt InTo S1 VaLuEs (1, 10)', 1)
            head, rows = pipe.execute('SeLeCt iD FrOm s1')
            check('S1a4 混合大小写查询', rows == [['1']], (head, rows))
            pipe.execute('ROLLBACK')
            # 注释三种风格（分号必须在注释之前，注释吞掉行内其余内容）
            expect_ok(pipe, 'S1b1 行注释 -- 与语句同行',
                      "INSERT INTO s1 VALUES (2, 20); -- 尾随注释", 1)
            expect_ok(pipe, 'S1b2 # 注释', "INSERT INTO s1 VALUES (3, 30); #井号注释", 1)
            head, rows = pipe.execute("/* 前缀 */ SELECT id FROM s1 WHERE id = 2")
            check('S1b3 块注释前缀', head.startswith('RESULT\t') and rows == [['2']], (head, rows))
            head, rows = pipe.execute("SELECT id FROM s1 /* 行内\n跨行 */ WHERE id = 3")
            check('S1b4 跨行块注释', rows == [['3']], (head, rows))
            # 串内分号与转义引号
            expect_ok(pipe, 'S1c1 建转义测试表',
                      "CREATE TABLE q (id BIGINT PRIMARY KEY, s VARCHAR(16))", 0)
            expect_ok(pipe, "S1c2 串内分号", "INSERT INTO q VALUES (1, 'a;b')", 1)
            expect_ok(pipe, "S1c3 两个单引号转义", "INSERT INTO q VALUES (2, 'It''s')", 1)
            head, rows = pipe.execute("SELECT s FROM q WHERE id = 2")
            check('S1c4 转义引号还原', rows == [["It's"]], rows)
            # 多行 SQL 与语句中注释
            pipe.raw("INSERT INTO q VALUES (3,\n'多行');\n")
            check('S1d1 多行 SQL', pipe.line() == 'OK\t1')
            pipe.raw("SELECT -- 中途注释\n id FROM q WHERE id = 3;\n")
            first = pipe.line()
            body = [pipe.line()]
            while not body[-1].startswith('END\t'):
                body.append(pipe.line())
            check('S1d2 语句中注释', first.startswith('RESULT\t') and body[0] == '3', (first, body))
            # 反引号标识符（含空格）
            expect_ok(pipe, 'S1e1 反引号表名含空格',
                      'CREATE TABLE `my table` (id BIGINT PRIMARY KEY)', 0)
            expect_ok(pipe, 'S1e2 反引号表插入', 'INSERT INTO `my table` VALUES (7)', 1)
            head, rows = pipe.execute('SELECT id FROM `my table`')
            check('S1e3 反引号表查询', rows == [['7']], rows)
            # 一行两条语句
            pipe.raw("INSERT INTO q VALUES (4, 'x'); INSERT INTO q VALUES (5, 'y');\n")
            check('S1f1 一行两语句两个响应', pipe.line() == 'OK\t1' and pipe.line() == 'OK\t1')
            # 注释行：无协议响应
            pipe.raw("-- 纯注释行\n")
            pipe.raw("SELECT id FROM q WHERE id = 4;\n")
            first = pipe.line()
            check('S1g1 注释行无响应（直接得到查询结果）', first.startswith('RESULT\t'), first)
            while not pipe.line().startswith('END\t'):
                pass
            # 未终止字符串：EOF 时报错并正常退出
            pipe.raw("INSERT INTO q VALUES (6, '未闭合\n")
            pipe.process.stdin.close()
            code = pipe.process.wait(timeout=20)
            out1 = pipe.line()
            check('S1h1 未终止字符串报 SYNTAX_ERROR', out1.startswith('ERROR\tSYNTAX_ERROR\t'), out1)
            check('S1h2 未终止字符串后正常退出(exit=0)', code == 0, code)
        with Pipe(directory) as pipe:
            pipe.raw("/* 未闭合块注释\n")
            pipe.process.stdin.close()
            code = pipe.process.wait(timeout=20)
            first = pipe.line()
            check('S1h3 未终止块注释报 SYNTAX_ERROR', first.startswith('ERROR\tSYNTAX_ERROR\t'), first)
            check('S1h4 未终止块注释后正常退出', code == 0, code)
        # --mode 控制行默认关闭：当作注释，无响应
        with Pipe(directory) as pipe:
            pipe.raw("--mode ann\n")
            head, rows = pipe.execute('SELECT id FROM `my table`')
            check('S1i1 默认关闭模式控制行', head.startswith('RESULT\t') and rows == [['7']], (head, rows))
            # 控制行开启但位于未完成语句之后：仍是注释
        with Pipe(directory, extra=('--local-mode-controls',)) as pipe:
            pipe.raw("SELECT id FROM `my table`\n")
            pipe.raw("--mode ann\n")
            pipe.raw(";\n")
            first = pipe.line()
            check('S1i2 未完成语句后的 --mode 行不是控制行', first.startswith('RESULT\t'), first)
            body = []
            while True:
                row = pipe.line()
                body.append(row)
                if row.startswith('END\t'):
                    break
            check('S1i3 该语句正常执行', body[0] == '7', body)
            # 完整语句之间的控制行
            pipe.execute('SELECT id FROM `my table`;')
            pipe.raw('--mode ann\n')
            check('S1i4 完整语句之间控制行 OK\\t0', pipe.line() == 'OK\t0')
        # COMMIT WORK 不支持
        with Pipe(directory) as pipe:
            expect_error(pipe, 'S1j1 COMMIT WORK 报 SYNTAX_ERROR', 'COMMIT WORK', 'SYNTAX_ERROR')
    finally:
        temp.cleanup()

    # ================= S2：类型边界与语义 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S2a1 BIGINT 边界表', 'CREATE TABLE b (id BIGINT PRIMARY KEY, v BIGINT)', 0)
            expect_ok(pipe, 'S2a2 2^63-1 接受', 'INSERT INTO b VALUES (1, 9223372036854775807)', 1)
            expect_ok(pipe, 'S2a3 -2^63 接受', 'INSERT INTO b VALUES (2, -9223372036854775808)', 1)
            expect_error(pipe, 'S2a4 2^63 溢出拒绝', 'INSERT INTO b VALUES (3, 9223372036854775808)', 'TYPE_ERROR')
            expect_error(pipe, 'S2a5 -2^63-1 溢出拒绝', 'INSERT INTO b VALUES (3, -9223372036854775809)', 'TYPE_ERROR')
            expect_error(pipe, 'S2a6 20 位数字拒绝', 'INSERT INTO b VALUES (3, 10000000000000000000)', 'TYPE_ERROR')
            expect_error(pipe, 'S2a7 小数给 BIGINT', 'INSERT INTO b VALUES (3, 1.0)', 'TYPE_ERROR')
            expect_ok(pipe, 'S2a8 前导零', 'INSERT INTO b VALUES (4, 0009)', 1)
            head, rows = pipe.execute('SELECT v FROM b WHERE id = 4')
            check('S2a9 前导零值为 9', rows == [['9']], rows)
            head, rows = pipe.execute('SELECT v FROM b WHERE id = 1')
            check('S2a10 2^63-1 精确读回', rows == [['9223372036854775807']], rows)
            # VARCHAR Unicode 计长
            expect_ok(pipe, 'S2b1 VARCHAR(4) 表', 'CREATE TABLE u (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_ok(pipe, 'S2b2 4 个汉字接受（按字符计）', "INSERT INTO u VALUES (1, '四个汉字')", 1)
            expect_error(pipe, 'S2b3 5 个汉字拒绝', "INSERT INTO u VALUES (2, '五个汉字符啊')", 'TYPE_ERROR')
            expect_ok(pipe, 'S2b4 空串接受', "INSERT INTO u VALUES (3, '')", 1)
            head, rows = pipe.execute("SELECT s FROM u WHERE id = 3")
            check('S2b5 空串读回', rows == [['']], rows)
            expect_ok(pipe, "S2b6 表情符号 1 字符", "INSERT INTO u VALUES (4, '😀')", 1)
            # 码点比较与排序
            expect_ok(pipe, 'S2c1 码点表', 'CREATE TABLE g (id BIGINT PRIMARY KEY, s VARCHAR(4))', 0)
            expect_ok(pipe, 'S2c2 插入汉字', "INSERT INTO g VALUES (1, '一'), (2, '三'), (3, '二'), (4, 'a')", 4)
            head, rows = pipe.execute("SELECT id FROM g WHERE s > 'a'")
            check('S2c3 汉字 > 拉丁（码点序，无序比较）',
                  sorted(int(r[0]) for r in rows) == [1, 2, 3], rows)
            head, rows = pipe.execute('SELECT id FROM g ORDER BY s')
            check('S2c4 ORDER BY 码点序 a<一<三<二', [r[0] for r in rows] == ['4', '1', '2', '3'], rows)
            # 非法向量全谱
            expect_ok(pipe, 'S2d1 向量表', 'CREATE TABLE w (id BIGINT PRIMARY KEY, v VECTOR(2))', 0)
            expect_ok(pipe, 'S2d2 科学计数法向量', "INSERT INTO w VALUES (1, '[1e2, -2.5]')", 1)
            for label, vec in (('NaN', '[NaN, 0]'), ('Infinity', '[Infinity, 0]'),
                               ('溢出', '[1e40, 0]'), ('维度多', '[0,0,0]'), ('维度少', '[0]'),
                               ('空数组', '[]'), ('布尔', '[true, 0]'),
                               ('嵌套', '[[0], 0]'), ('JSON 语法错', '[1,2,]'),
                               ('裸点号', '[.5, 0]')):
                expect_error(pipe, f'S2d3 拒绝 {label}', f"INSERT INTO w VALUES (2, '{vec}')", 'INVALID_VECTOR')
            expect_error(pipe, 'S2d3a 引号嵌套向量串被词法拒绝', "INSERT INTO w VALUES (2, '['a', 0]')", 'SYNTAX_ERROR')
            expect_error(pipe, 'S2d4 向量列为数字字面量', 'INSERT INTO w VALUES (2, 123)', 'TYPE_ERROR')
            expect_error(pipe, 'S2d5 WHERE 中 L2_DISTANCE 不支持', "SELECT id FROM w WHERE L2_DISTANCE(v, 123) > 0", 'UNSUPPORTED')
            expect_error(pipe, 'S2d6 WHERE 中 L2_DISTANCE 用于非向量列', "SELECT id FROM u WHERE L2_DISTANCE(s, '[0]') > 0", 'UNSUPPORTED')
            expect_error(pipe, 'S2d7 WHERE 向量等值比较', "SELECT id FROM w WHERE v = '[1,2]'", 'UNSUPPORTED')
            expect_error(pipe, 'S2d8 UPDATE WHERE 向量比较', "UPDATE w SET v='[1,1]' WHERE v = '[1,2]'", 'UNSUPPORTED')
            expect_error(pipe, 'S2d9 OR 不支持（尾随 SQL）', 'SELECT id FROM b WHERE id = 1 OR id = 2', 'SYNTAX_ERROR')
            expect_error(pipe, 'S2d10 IN 不支持', 'SELECT id FROM b WHERE id IN (1, 2)', 'UNSUPPORTED')
            expect_error(pipe, 'S2d11 NOT 不支持', 'SELECT id FROM b WHERE NOT id = 1', 'UNSUPPORTED')
            expect_error(pipe, 'S2d12 聚合不支持', 'SELECT COUNT(id) FROM b', 'SYNTAX_ERROR')
            expect_error(pipe, 'S2d13 JOIN 不支持', 'SELECT id FROM b JOIN u ON b.id = u.id', 'SYNTAX_ERROR')
            expect_error(pipe, 'S2d14 别名必须 AS', 'SELECT id n FROM b', 'SYNTAX_ERROR')
            expect_error(pipe, 'S2d15 ORDER BY * 不支持', 'SELECT id FROM b ORDER BY *', 'UNSUPPORTED')
            expect_error(pipe, 'S2d16 * 不能有别名', 'SELECT * AS x FROM b', 'SYNTAX_ERROR')
            expect_error(pipe, 'S2d17 重复别名', 'SELECT id AS d, v AS d FROM b', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2d18 ORDER BY 未知列', 'SELECT id FROM b ORDER BY ghost', 'NOT_FOUND')
            expect_error(pipe, 'S2d19 LIMIT 负数', 'SELECT id FROM b LIMIT -1', 'TYPE_ERROR')
            expect_error(pipe, 'S2d20 LIMIT 小数', 'SELECT id FROM b LIMIT 1.5', 'TYPE_ERROR')
            # DDL 约束
            expect_error(pipe, 'S2e1 表已存在', 'CREATE TABLE b (id BIGINT PRIMARY KEY)', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2e2 大小写折叠后仍冲突', 'CREATE TABLE B (id BIGINT PRIMARY KEY)', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2e3 重复列名', 'CREATE TABLE c1 (id BIGINT PRIMARY KEY, id BIGINT)', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2e4 主键须 BIGINT', 'CREATE TABLE c2 (id VARCHAR(4) PRIMARY KEY)', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2e5 两个向量列', 'CREATE TABLE c3 (id BIGINT PRIMARY KEY, a VECTOR(2), b VECTOR(2))', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2e6 缺主键', 'CREATE TABLE c4 (id BIGINT)', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2e7 两个主键', 'CREATE TABLE c5 (id BIGINT PRIMARY KEY, v BIGINT PRIMARY KEY)', 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2e8 VARCHAR(0)', 'CREATE TABLE c6 (id BIGINT PRIMARY KEY, s VARCHAR(0))', 'TYPE_ERROR')
            expect_error(pipe, 'S2e9 VECTOR(0)', 'CREATE TABLE c7 (id BIGINT PRIMARY KEY, v VECTOR(0))', 'TYPE_ERROR')
            expect_error(pipe, 'S2e10 索引须向量列', 'CREATE VECTOR INDEX ix ON b(v)', 'TYPE_ERROR')
            expect_error(pipe, 'S2e11 索引用未知列', 'CREATE VECTOR INDEX ix ON w(ghost)', 'NOT_FOUND')
            expect_error(pipe, 'S2e12 索引用未知表', 'CREATE VECTOR INDEX ix ON ghost(v)', 'NOT_FOUND')
            expect_ok(pipe, 'S2e13 建索引', 'CREATE VECTOR INDEX iw ON w(v)', 0)
            expect_ok(pipe, 'S2e13a 第二张向量表', 'CREATE TABLE c9 (id BIGINT PRIMARY KEY, v VECTOR(2))', 0)
            expect_error(pipe, 'S2e14 跨表索引名冲突（本地约定全局唯一）',
                         'CREATE VECTOR INDEX iw ON c9(v)', 'CONSTRAINT_ERROR')
            # 失败 DDL 无残留
            expect_error(pipe, 'S2f1 失败建表', 'CREATE TABLE c8 (id BIGINT PRIMARY KEY, id2 BIGINT, id2 BIGINT)', 'CONSTRAINT_ERROR')
            expect_ok(pipe, 'S2f2 重建同名表成功', 'CREATE TABLE c8 (id BIGINT PRIMARY KEY, id2 BIGINT)', 0)
            # 未知表/列
            expect_error(pipe, 'S2g1 查未知表', 'SELECT id FROM ghost', 'NOT_FOUND')
            expect_error(pipe, 'S2g2 插未知表', "INSERT INTO ghost VALUES (1, 'x')", 'NOT_FOUND')
            expect_error(pipe, 'S2g3 改未知表', "UPDATE ghost SET s='x'", 'NOT_FOUND')
            expect_error(pipe, 'S2g4 删未知表', 'DELETE FROM ghost', 'NOT_FOUND')
            expect_error(pipe, 'S2g5 未知列条件', "SELECT id FROM b WHERE ghost = 1", 'NOT_FOUND')
            # INSERT 列清单
            expect_ok(pipe, 'S2h1 列清单换序插入', "INSERT INTO u (s, id) VALUES ('换序', 9)", 1)
            head, rows = pipe.execute("SELECT s FROM u WHERE id = 9")
            check('S2h2 换序值落位正确', rows == [['换序']], rows)
            expect_error(pipe, 'S2h3 列清单缺列', "INSERT INTO u (id) VALUES (10)", 'TYPE_ERROR')
            expect_error(pipe, 'S2h4 列清单重复列', "INSERT INTO u (id, id) VALUES (10, 10)", 'TYPE_ERROR')
            expect_error(pipe, 'S2h5 值个数不符', "INSERT INTO u VALUES (10, 'a', 'b')", 'TYPE_ERROR')
            # UPDATE 语义
            expect_error(pipe, 'S2i1 重复赋值', "UPDATE u SET s='a', s='b' WHERE id = 9", 'CONSTRAINT_ERROR')
            expect_error(pipe, 'S2i2 主键更新不支持', 'UPDATE u SET id = 99 WHERE id = 9', 'UNSUPPORTED')
            expect_ok(pipe, 'S2i3 相同值 UPDATE 影响 1 行（本地约定按命中计）', "UPDATE u SET s='换序' WHERE id = 9", 1)
            expect_ok(pipe, 'S2i4 零行 UPDATE 影响 0 行', "UPDATE u SET s='x' WHERE id = 999", 0)
            expect_ok(pipe, 'S2i5 全表 UPDATE', "UPDATE u SET s='全改'", 4)
            expect_ok(pipe, 'S2i6 全表 DELETE', 'DELETE FROM u', 4)
            head, rows = pipe.execute('SELECT id FROM u')
            check('S2i7 删空后查询为空', rows == [], rows)
            # 向量相同值更新不破坏正确性
            expect_ok(pipe, 'S2j1 向量相同值更新', "UPDATE w SET v='[100, -2.5]' WHERE id = 1", 1)
            head, rows = pipe.execute("SELECT id FROM w ORDER BY L2_DISTANCE(v, '[100,-2.5]') LIMIT 1")
            check('S2j2 更新后 TopK 正确', rows == [['1']], rows)
            # SELECT 常量报错（表达式只支持列/距离）
            expect_error(pipe, 'S2k1 SELECT 常量不支持', 'SELECT 1 FROM b', 'SYNTAX_ERROR')
        with Pipe(directory) as pipe:
            head, rows = pipe.execute('SELECT id FROM u ORDER BY id')
            check('S2l1 全删持久化（重启后仍空）', rows == [], rows)
            head, rows = pipe.execute('SELECT v FROM b WHERE id = 2')
            check('S2l2 BIGINT 边界值持久化', rows == [['-9223372036854775808']], rows)
    finally:
        temp.cleanup()

    # ================= S3：输出转义与格式 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            expect_ok(pipe, 'S3a1 建转义表', 'CREATE TABLE e (id BIGINT PRIMARY KEY, s VARCHAR(16))', 0)
            expect_ok(pipe, 'S3a2 反斜杠', "INSERT INTO e VALUES (1, 'a\\b')", 1)
            expect_ok(pipe, 'S3a3 制表符', "INSERT INTO e VALUES (2, 'a\tb')", 1)
            pipe.raw("INSERT INTO e VALUES (3, 'a\nb');\n")
            check('S3a4 换行插入', pipe.line() == 'OK\t1')
            pipe.raw("INSERT INTO e VALUES (4, 'a\rb');\n")
            check('S3a5 回车插入', pipe.line() == 'OK\t1')
            expect_ok(pipe, 'S3a6 单引号内容', "INSERT INTO e VALUES (5, 'It''s')", 1)
            head, rows = pipe.execute('SELECT s FROM e ORDER BY id')
            # 注记：Windows 文本 stdin 的 universal-newline 会在传输层把裸 \r 翻译为 \n；
            # Linux（官方目标平台）无此转换，\r 原样到达并被正确转义输出。
            if os.name == 'nt':
                cr_row = ['a\\nb']
                note = 'CR 行经 Windows 传输层变 \\n'
            else:
                cr_row = ['a\\rb']
                note = 'CR 行原样保留并转义'
            expected = [['a\\\\b'], ['a\\tb'], ['a\\nb'], cr_row, ["It's"]]
            check(f'S3b1 转义输出精确匹配（{note}）', rows == expected, rows)
            check('S3b2 普通操作无 stderr 输出', pipe.stderr_lines == [], pipe.stderr_lines[:3])
            # 向量紧凑 JSON 精确文本
            expect_ok(pipe, 'S3c1 向量表', 'CREATE TABLE vec (id BIGINT PRIMARY KEY, v VECTOR(2))', 0)
            expect_ok(pipe, 'S3c2 float32 舍入值插入', "INSERT INTO vec VALUES (1, '[0.1, 0.2]')", 1)
            head, rows = pipe.execute('SELECT v FROM vec')
            exp_text = '[' + ','.join(repr(f32(v)) for v in (0.1, 0.2)) + ']'
            check('S3c3 向量输出为 float32 紧凑 JSON', rows == [[exp_text]], (rows, exp_text))
            # SELECT * 列顺序
            expect_ok(pipe, 'S3d1 多列表', 'CREATE TABLE star (id BIGINT PRIMARY KEY, a VECTOR(2), s VARCHAR(4))', 0)
            expect_ok(pipe, 'S3d2 插入', "INSERT INTO star VALUES (1, '[1,2]', 'x')", 1)
            head, rows = pipe.execute('SELECT * FROM star')
            check('S3d3 SELECT * 列顺序为定义序', head == 'RESULT\tid\ta\ts' and rows == [['1', '[1.0,2.0]', 'x']], (head, rows))
        with Pipe(directory) as pipe:
            head, rows = pipe.execute('SELECT s FROM e ORDER BY id')
            check('S3e1 转义内容重启后不变',
                  rows == [['a\\\\b'], ['a\\tb'], ['a\\nb'], cr_row, ["It's"]], rows)
    finally:
        temp.cleanup()

    # ================= S4：有限内存排序与临时文件 =================
    temp, directory = fresh()
    try:
        # 启动即清理陈旧临时文件
        for junk in ('sort-stale.tmp', 'modify-stale.tmp', 'transaction.stage',
                     'transaction.idx', 'checkpoint.wal'):
            (directory / junk).write_bytes(b'junk')
        with Pipe(directory) as pipe:
            leftovers = [p.name for p in directory.iterdir() if p.name.endswith('.tmp') or p.name.startswith('transaction.') or p.name == 'checkpoint.wal']
            check('S4a1 启动清理陈旧临时文件', leftovers == [], leftovers)
            expect_ok(pipe, 'S4b1 建排序表', 'CREATE TABLE srt (id BIGINT PRIMARY KEY, v BIGINT)', 0)
            rng = random.Random(7)
            values = rng.sample(range(1_000_000), 400)
            for batch in range(4):
                chunk = values[batch * 100:(batch + 1) * 100]
                expect_ok(pipe, f'S4b2 批 {batch + 1} 插入 100 行',
                          'INSERT INTO srt VALUES ' + ','.join(f'({v},{v})' for v in chunk), 100)
            truth = sorted(values)
            head, rows = pipe.execute('SELECT v FROM srt ORDER BY v')
            check('S4c1 4KiB 预算全量排序正确（外部归并）',
                  [r[0] for r in rows] == [str(v) for v in truth], f'rows={len(rows)}')
            leftover = glob.glob(str(directory / 'sort-*.tmp'))
            check('S4c2 查询后无 sort 临时文件残留', leftover == [], leftover)
            head, rows = pipe.execute('SELECT v FROM srt ORDER BY v DESC LIMIT 10')
            check('S4c3 TopK 剪枝 DESC LIMIT 10', [r[0] for r in rows] == [str(v) for v in sorted(values, reverse=True)[:10]], rows)
            # 重复值 DESC 并列按主键升序（小预算下）
            expect_ok(pipe, 'S4d1 并列表', 'CREATE TABLE tie (id BIGINT PRIMARY KEY, v BIGINT)', 0)
            expect_ok(pipe, 'S4d2 插入并列值', 'INSERT INTO tie VALUES (3,5), (1,5), (2,5), (4,1)', 4)
            head, rows = pipe.execute('SELECT id FROM tie ORDER BY v DESC')
            check('S4d3 DESC 并列按主键升序', [r[0] for r in rows] == ['1', '2', '3', '4'], rows)
            head, rows = pipe.execute('SELECT id FROM tie ORDER BY v ASC')
            check('S4d4 ASC 并列按主键升序', [r[0] for r in rows] == ['4', '1', '2', '3'], rows)
            # 多排序键
            expect_ok(pipe, 'S4e1 多键表', 'CREATE TABLE mk (id BIGINT PRIMARY KEY, tag VARCHAR(4), v BIGINT)', 0)
            expect_ok(pipe, 'S4e2 插入', "INSERT INTO mk VALUES (1,'b',2), (2,'a',9), (3,'b',1), (4,'a',9), (5,'c',0)", 5)
            head, rows = pipe.execute('SELECT id FROM mk ORDER BY tag ASC, v DESC')
            check('S4e3 多键排序 (tag升, value降, 主键升)', [r[0] for r in rows] == ['2', '4', '1', '3', '5'], rows)
        # 差分：同一数据默认 4MiB 预算 → 输出一致
        with Pipe(directory) as pipe:
            head, rows = pipe.execute('SELECT v FROM srt ORDER BY v LIMIT 50')
            check('S4f1 默认预算输出与 4KiB 预算一致',
                  [r[0] for r in rows] == [str(v) for v in truth[:50]], None)
            # 小预算 + 索引：过滤缓存预算回退
            expect_ok(pipe, 'S4g1 过滤表', 'CREATE TABLE wf (id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(3))', 0)
            rng2 = random.Random(11)
            spec = []
            for i in range(1, 301):
                vec = f"[{rng2.uniform(-9,9):.4f},{rng2.uniform(-9,9):.4f},{rng2.uniform(-9,9):.4f}]"
                spec.append((i, 'a' if i <= 100 else 'b', vec))
            expect_ok(pipe, 'S4g2 插入 300 行',
                      'INSERT INTO wf VALUES ' + ','.join(f"({i},'{t}','{v}')" for i, t, v in spec), 300)
            expect_ok(pipe, 'S4g3 建索引', 'CREATE VECTOR INDEX wfi ON wf(v)', 0)
            # tag='a'（100 行 → 缓存路径）与 tag='b'（200 行 → 预算回退路径）
            for tag, count in (('a', 100), ('b', 200)):
                q = '[0,0,0]'
                qv = [f32(0.0)] * 3
                truth_pairs = sorted((math.dist([f32(x) for x in json.loads(v)], qv), i)
                                     for i, t, v in spec if t == tag)[:5]
                head, rows = pipe.execute(
                    f"SELECT id FROM wf WHERE tag='{tag}' ORDER BY L2_DISTANCE(v, '{q}') LIMIT 5")
                check(f'S4g4 小预算过滤 TopK tag={tag}（{"缓存" if count==100 else "回退"}路径）',
                      [r[0] for r in rows] == [str(pk) for _, pk in truth_pairs], rows)
            # K 超过 max_keep（默认 8192）→ 通用执行器返回全部
            head, rows = pipe.execute("SELECT id FROM wf ORDER BY L2_DISTANCE(v, '[0,0,0]') LIMIT 9000")
            all_pairs = sorted((math.dist([f32(x) for x in json.loads(v)], [f32(0.0)] * 3), i)
                               for i, _, v in spec)
            check('S4h1 K 超过原生上限回退通用执行器（全量正确）',
                  [r[0] for r in rows] == [str(pk) for _, pk in all_pairs], f'rows={len(rows)}')
    finally:
        temp.cleanup()

    # ================= S5：超大维度边界 =================
    temp, directory = fresh()
    try:
        with Pipe(directory) as pipe:
            zero65536 = '[' + ','.join(['0'] * 65536) + ']'
            one65536 = '[' + ','.join(['1'] * 65536) + ']'
            expect_ok(pipe, 'S5a1 VECTOR(65536) 建表', f'CREATE TABLE d1 (id BIGINT PRIMARY KEY, v VECTOR(65536))', 0)
            expect_ok(pipe, 'S5a2 插入两行', f"INSERT INTO d1 VALUES (1, '{zero65536}'), (2, '{one65536}')", 2)
            expect_ok(pipe, 'S5a3 建索引（65536 为原生上限内）', 'CREATE VECTOR INDEX d1i ON d1(v)', 0)
            head, rows = pipe.execute(f"SELECT id FROM d1 ORDER BY L2_DISTANCE(v, '{zero65536}') LIMIT 1")
            check('S5a4 65536 维 TopK 正确', rows == [['1']], rows[:1])
        with Pipe(directory) as pipe:
            zero65537 = '[' + ','.join(['0'] * 65537) + ']'
            expect_ok(pipe, 'S5b1 VECTOR(65537) 建表（超原生上限）', 'CREATE TABLE d2 (id BIGINT PRIMARY KEY, v VECTOR(65537))', 0)
            expect_ok(pipe, 'S5b2 插入', f"INSERT INTO d2 VALUES (1, '{zero65537}')", 1)
            expect_ok(pipe, 'S5b3 建索引（应成功，实际走 Python 回退）', 'CREATE VECTOR INDEX d2i ON d2(v)', 0)
            head, rows = pipe.execute(f"SELECT id FROM d2 ORDER BY L2_DISTANCE(v, '{zero65537}') LIMIT 1")
            check('S5b4 65537 维回退路径正确', rows == [['1']], rows[:1])
            # L2_DISTANCE 不能用于 WHERE（本地语法范围）：应报错而非静默执行
            expect_error(pipe, 'S5b5 WHERE 中 L2_DISTANCE 报 UNSUPPORTED',
                         f"SELECT id FROM d2 WHERE L2_DISTANCE(v, '{zero65537}') = 0", 'UNSUPPORTED')
    finally:
        temp.cleanup()

    # ================= S6：启动参数 =================
    temp, directory = fresh()
    try:
        proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'minivectordb', '--data-dir', str(directory),
             '--sort-memory-mib', '0.001'],
            cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        proc.stdin.close()
        out = proc.stdout.read()
        code = proc.wait(timeout=30)
        check('S6a1 非法排序预算：无 READY、无 stdout 输出', out == '' and code != 0, (out[:80], code))
        proc = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-m', 'minivectordb', '--data-dir', str(directory),
             '--no-such-flag'],
            cwd=ROOT, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8')
        proc.stdin.close()
        code = proc.wait(timeout=30)
        check('S6a2 未知参数：argparse 退出码 2', code == 2, code)
    finally:
        temp.cleanup()

    print(f'\nSUMMARY passed={len(PASSED)} failed={len(FAILED)}')
    if FAILED:
        print('FAILED:', *FAILED, sep='\n  ')
        sys.exit(1)


if __name__ == '__main__':
    main()
