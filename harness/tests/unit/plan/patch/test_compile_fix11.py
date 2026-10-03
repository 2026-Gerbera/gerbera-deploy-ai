from ddak.plan.patch.check import build_patch, check_patch


def test_future_import_order_rejected(tmp_path):
    old = 'from __future__ import annotations\nSECRET_KEY = "' + 'dev"\n'
    new = 'import os\nfrom __future__ import annotations\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    (tmp_path / "config.py").write_text(old)
    result = check_patch(tmp_path, build_patch({"config.py": (old, new)}))
    assert not result.passed and "syntax" in {v.code for v in result.violations}


def test_815ea60_dict_key_fix_keeps_secret_values_blocked(tmp_path):
    old = 'import os\nconfig = {"SECRET_KEY": "' + 'dev"}\n'
    safe = 'import os\nconfig = {"SECRET_KEY": os.environ["SECRET_KEY"]}\n'
    (tmp_path / "config.py").write_text(old)
    assert check_patch(tmp_path, build_patch({"config.py": (old, safe)})).passed
    unsafe = 'import os\nconfig = {"SECRET_KEY": "' + 'different-dev"}\n'
    assert not check_patch(tmp_path, build_patch({"config.py": (old, unsafe)})).passed
