import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
ADMIN_PATH = BACKEND / "routes" / "admin.py"
sys.path.insert(0, str(BACKEND))


def _load_admin_module():
    spec = importlib.util.spec_from_file_location("planning_admin", ADMIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compact_task_name_strips_home_shortcuts():
    admin = _load_admin_module()
    samples = {
        "请帮我制作一个智慧农业平台": "智慧农业平",
        "请根据以下主题生成一份结构完整、可演示的 PPT：东农智能体产品发布": "东农智能体",
        "请根据以下需求生成高质量图像，并先完善视觉提示词：未来农业城市": "未来农业城",
    }
    for prompt, expected in samples.items():
        assert admin._compact_task_name(prompt) == expected
        assert 3 <= len(admin._compact_task_name(prompt)) <= 5


def test_compact_task_name_has_safe_fallback():
    admin = _load_admin_module()
    assert admin._compact_task_name("!!") == "任务规划"
