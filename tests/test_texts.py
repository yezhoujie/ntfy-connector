"""固定文案的字符串表：两列 key 集合相同、英文列不含汉字、取值与语言解析。"""

import os
import re
import sys
import unittest

import texts

HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]")  # 汉字 + 全角标点 / 方括号


class TableTest(unittest.TestCase):
    def test_both_columns_have_exactly_the_same_keys(self):
        self.assertEqual(texts.LANGS, ("zh", "en"))
        zh, en = set(texts.TEXTS["zh"]), set(texts.TEXTS["en"])
        self.assertEqual(zh ^ en, set(), f"只在一列里的 key：{sorted(zh ^ en)}")
        self.assertGreater(len(zh), 80)

    def test_english_column_has_no_cjk(self):
        for key, value in texts.TEXTS["en"].items():
            self.assertIsNone(HAN.search(value), f"{key}: {value!r}")

    def test_placeholders_match_between_columns(self):
        names = re.compile(r"\{(\w+)\}")
        for key in texts.TEXTS["zh"]:
            self.assertEqual(set(names.findall(texts.TEXTS["zh"][key])), set(names.findall(texts.TEXTS["en"][key])), key)

    def test_t_formats_and_rejects_unknown(self):
        self.assertEqual(texts.t("prefix.answered", "zh"), "✅ 已回复 · ")
        self.assertEqual(texts.t("prefix.answered", "en"), "✅ Answered · ")
        self.assertIn("3", texts.t("validate.header", "en", n=3))
        with self.assertRaises(KeyError):
            texts.t("no.such.key", "en")
        with self.assertRaises(KeyError):
            texts.t("prefix.answered", "jp")

    def test_literal_braces_survive_when_no_format_args(self):
        # 提示里有字面的 {id, label, consequence}：不带参数取值时不能被 format 吃掉
        self.assertIn("{id, label, consequence}", texts.t("validate.hint.options", "zh"))

    def test_go_run_instructions_are_filled_with_the_real_launch_command(self):
        # 调用方没传 cli，t() 自己按当前进程补——call site 一律不动
        interpreter = "python" if sys.platform == "win32" else "python3"
        for lang in texts.LANGS:
            rendered = texts.t("cli.away.not_enabled", lang)
            self.assertIn(f'{interpreter} "', rendered)
            self.assertIn("away on", rendered)
            self.assertNotIn("ntfy-connector away on", rendered)

    def test_self_reference_and_validation_titles_stay_literal(self):
        # 自称与校验报错标题是「这条命令叫什么」，不是「去跑它」，不参与替换
        self.assertIn("ntfy-connector：", texts.t("cli.confirm.done_prompt", "zh", slot="s1"))
        self.assertTrue(texts.t("validate.header", "en", n=1).startswith("ntfy-connector ask:"))
        self.assertTrue(texts.t("validate.notify.header", "zh", n=1).startswith("ntfy-connector notify:"))


class SelfCommandTest(unittest.TestCase):
    def test_quotes_the_absolute_path(self):
        self.assertEqual(texts.self_command("/a b/c.py", platform="linux"), 'python3 "/a b/c.py"')

    def test_win32_uses_python_without_the_3(self):
        result = texts.self_command("/a/b.py", platform="win32")
        self.assertTrue(result.startswith('python "'))

    def test_posix_escapes_embedded_quotes_and_backslashes(self):
        result = texts.self_command('/a "quoted"\\b.py', platform="linux")
        self.assertIn('\\"quoted\\"', result)
        self.assertIn("\\\\b.py", result)

    def test_win32_escapes_only_quotes_not_backslashes(self):
        result = texts.self_command('C:\\a "b"\\c.py', platform="win32")
        self.assertIn('\\"b\\"', result)
        self.assertIn("C:\\a", result)  # 反斜杠是路径分隔符，win32 下不转义

    def test_empty_entry_falls_back_to_a_placeholder_without_raising(self):
        self.assertEqual(
            texts.self_command("", platform="linux"),
            'python3 "<skill dir>/scripts/ntfy_connector.py"',
        )

    def test_explicit_win32_entry_is_absolutized_with_windows_semantics_not_the_host_platform(self):
        # 宿主是非 win32 时，注入的 win32 路径已经是绝对路径（带盘符），不能被当成相对路径去拼宿主 cwd
        entry = 'C:\\a "b"\\c.py'
        result = texts.self_command(entry, platform="win32")
        self.assertEqual(result, 'python "' + entry.replace('"', '\\"') + '"')
        self.assertNotIn(os.getcwd(), result)


class ResolveTest(unittest.TestCase):
    def test_priority_explicit_then_env_then_default(self):
        self.assertEqual(texts.resolve("zh", "en"), "zh")
        self.assertEqual(texts.resolve(None, "zh"), "zh")
        self.assertEqual(texts.resolve(None, None), "en")
        self.assertEqual(texts.DEFAULT_LANG, "en")

    def test_invalid_values_fall_through(self):
        self.assertEqual(texts.resolve("jp", "zh"), "zh")  # 非法的显式值不生效（校验层会另外报错）
        self.assertEqual(texts.resolve(None, "zh-CN"), "en")  # 只认恰好 zh / en
        self.assertEqual(texts.resolve("", "EN"), "en")
        self.assertTrue(texts.is_lang("en"))
        self.assertFalse(texts.is_lang("En"))
        self.assertFalse(texts.is_lang(None))


if __name__ == "__main__":
    unittest.main()
