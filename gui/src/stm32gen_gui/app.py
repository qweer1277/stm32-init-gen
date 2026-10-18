"""GUI 入口（PySide6）。

用法::

    python -m stm32gen_gui            # 或
    stm32-init-gui                    # 安装后生成的入口脚本

注意：本模块**只在被调用时才导入 PySide6**，所以 ``import stm32gen_gui.app``
在没有装 GUI 依赖的环境里也不会失败。

### 为什么自检放在这一侧

``--selfcheck`` 曾经在核心库的 ``python -m stm32gen selfcheck`` 里，会连带
导入桥接层与 Qt。桌面界面拆成独立项目后，那意味着「只装了核心库」的环境
自检必然失败 —— 而那恰恰是它要支持的用法。判定「界面这一侧是否可用」的
检查，理应和界面住在同一个项目里。
"""

from __future__ import annotations

import sys


def _selfcheck() -> int:
    """检查 GUI 侧依赖是否齐备：核心库、桥接层往返转换、Qt 平台插件。"""
    # Windows 控制台在重定向时按本地代码页（简中为 GBK）编码。一个 GBK
    # 编不出的符号就会让整个自检抛 UnicodeEncodeError，用户看到的是
    # 「启动不了」而不是「某个字符打不出来」。替换成 ? 继续走完。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    ok = True

    # 核心库：界面没有它什么也做不了，但它不该被本包复制一份
    try:
        from stm32gen import __version__ as core_version
        from stm32gen.models import make_demo_config
    except Exception as exc:
        print(f"  [FAIL] 核心库 stm32-init-gen 不可用：{exc}")
        print("         请先安装：pip install -e ..\\stm32-init-gen")
        return 1
    print(f"  [ OK ] 核心库 stm32-init-gen {core_version}")

    # 桥接层：不依赖 Qt，最容易出错的是表单与模型的往返转换
    try:
        from stm32gen_gui import bridge
    except Exception as exc:
        print(f"  [FAIL] 桥接层加载失败：{exc}")
        return 1
    result = bridge.ConfigForm.from_config(make_demo_config()).to_config()
    if not result.ok:
        print(f"  [FAIL] 桥接往返转换失败：{result.format_errors()}")
        ok = False
    else:
        # 用 ASCII 箭头而不是 ↔：后者在 GBK 里没有码位
        print("  [ OK ] 桥接层（表单 -> 配置模型往返转换正常）")

    # 核心资源：全部模板与符号表。打包成 exe 后最容易丢的就是这两样 ——
    # 少了它们启动时不报错，只在点「生成」或「校验」那一刻炸，所以必须自检。
    try:
        from stm32gen.generator.render import OUTPUT_SPECS, list_templates
        from stm32gen.stdlib_ref import default_index

        templates = list_templates()
        index = default_index()
        if not templates:
            raise RuntimeError("模板目录为空")
        # 模板数必须与 OUTPUT_SPECS 一致：少一个就是打包时漏了 datas 里的
        # 某个 .j2，而那要到用户点「生成」时才会以 TemplateNotFound 暴露。
        if len(templates) != len(OUTPUT_SPECS):
            raise RuntimeError(
                f"模板数 {len(templates)} 与 OUTPUT_SPECS 的 "
                f"{len(OUTPUT_SPECS)} 不一致，打包可能漏了模板文件"
            )
        # 抽几个真实符号验证内容可查，而不只是文件能读。
        # 覆盖三种前缀：RCC_（宏）、GPIO_（函数）、ADC_（新增外设）——
        # 若 symbols.json 在包里丢了，这里会立刻失败。
        for name in ("RCC_HSEConfig", "GPIO_Init", "ADC_Init", "ADC_Channel_3"):
            if not index.has_any(name):
                raise RuntimeError(f"符号表里查不到 {name}")
    except Exception as exc:
        print(f"  [FAIL] 核心资源不可用：{exc}")
        ok = False
    else:
        print(
            f"  [ OK ] 核心资源（模板 {len(templates)} 个 / "
            f"OUTPUT_SPECS {len(OUTPUT_SPECS)} 项，符号表可查询）"
        )

    # ADC 模块链：models.adc / adc_pins 是新增的，打包时若 static analysis
    # 没跟到（例如被写成运行期 import_string），exe 里会缺模块。
    # 这里显式导入一次，让打包与自检都把这条链当作硬依赖。
    try:
        from stm32gen.models import AdcConfig  # noqa: F401
        from stm32gen.models.adc_pins import DEFAULT_ADC_PINS  # noqa: F401

        if not DEFAULT_ADC_PINS:
            raise RuntimeError("ADC 通道/引脚表为空")
    except Exception as exc:
        print(f"  [FAIL] ADC 模块链不可用：{exc}")
        ok = False
    else:
        print("  [ OK ] ADC 模块链（models.adc / adc_pins）可导入")

    # Qt 本体：能构造出 QApplication 才算可用
    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:
        print(f"  [FAIL] PySide6 不可用：{exc}")
        return 1

    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    print(f"  [ OK ] Qt 平台插件 {app.platformName()}（QApplication 可构造）")
    return 0 if ok else 1


def main() -> int:
    """启动 GUI，返回进程退出码。"""
    if "--selfcheck" in sys.argv[1:]:
        return _selfcheck()

    from stm32gen_gui.main_window import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
