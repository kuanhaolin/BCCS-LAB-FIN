"""Tests for browser-side accounting print URL validation."""

import subprocess
import unittest
from pathlib import Path


class AccountPrintTargetTests(unittest.TestCase):
    def test_renderer_accepts_official_voucher_url_and_rejects_changed_targets(self):
        script = """
import { validAccountPrintTarget as valid } from "./browser/account_print_target.mjs";
const number = "111540300203";
const url = new URL("https://account.ncu.edu.tw/APSWIS_Q/TMP_PAG_Q/NCU33.asp?APPYYEAR=115&APYNO=111540300203&APYADD=&APPYSET=15");
if (!valid(url, number)) process.exit(1);
for (const changed of [
  url.href.replace("APPYSET=15", "APPYSET=11"),
  url.href.replace("NCU33.asp", "Other.asp"),
  url.href + "&unexpected=1",
  url.href.replace("account.ncu.edu.tw", "example.com"),
]) {
  if (valid(new URL(changed), number)) process.exit(2);
}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=Path(__file__).parent.parent, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
