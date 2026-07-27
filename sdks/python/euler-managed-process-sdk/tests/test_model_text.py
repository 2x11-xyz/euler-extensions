from pathlib import Path
import sys
import unittest


SDK_SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SDK_SOURCE))

from euler_managed_process_sdk import extension_model_text_is_format_safe


FORMAT_SPOOF_RANGES = (
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x2028, 0x2029),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)


class ModelTextTests(unittest.TestCase):
    def test_rejects_the_complete_frozen_unicode_17_format_set(self):
        rejected = 0
        for start, end in FORMAT_SPOOF_RANGES:
            for code_point in range(start, end + 1):
                with self.subTest(code_point=f"U+{code_point:04X}"):
                    self.assertFalse(
                        extension_model_text_is_format_safe(chr(code_point))
                    )
                rejected += 1

        self.assertEqual(rejected, 172)
        self.assertTrue(extension_model_text_is_format_safe("reserved\u2065"))

    def test_leaves_non_format_rules_to_the_calling_boundary(self):
        self.assertTrue(extension_model_text_is_format_safe(""))
        self.assertTrue(extension_model_text_is_format_safe("line\nbreak"))
        with self.assertRaises(TypeError):
            extension_model_text_is_format_safe(None)


if __name__ == "__main__":
    unittest.main()
